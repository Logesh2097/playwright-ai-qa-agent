"""Nodes 1 to 7: from the instruction to approved test cases."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from langgraph.types import interrupt

from app.agent.llm.base import LLMError
from app.agent.nodes.generate_scripts import CaseToScript, ScriptContext, generate_scripts
from app.agent.schemas.requirement import RequirementAnalysis
from app.agent.schemas.script import ScriptConversionError, to_script
from app.browser.events import LogRecorder, Recorders
from app.browser.factory import launch_options
from app.browser.login import LoginRecipe
from app.core.errors import ConflictError
from app.core.security.permissions import Permission
from app.core.security.url_safety import DestinationPolicy
from app.models import Approval, TestPlan, WorkflowRun
from app.repositories.instructions import InstructionRepository, PlanRepository
from app.repositories.projects import ApplicationRepository, EnvironmentRepository
from app.repositories.test_cases import SuiteRepository, TestCaseRepository
from app.repositories.workflows import ApprovalRepository
from app.services.automation import load_environment_facts, parse_script
from app.services.browser_checks import DbRecorder
from app.services.instructions import (
    ANALYSE_JOB,
    AnalysisRunner,
    InstructionService,
    apply_validation,
    load_target,
    plan_scope,
)
from app.services.llm_usage import require_budget
from app.services.test_cases import (
    GenerationRunner,
    approve_case,
    begin_generation,
    build_facts,
    load_views,
    write_version,
)
from app.testing.automation import changes_state
from app.testing.automation_checks import check_script
from app.testing.case_validation import CaseIssue, validate_case
from app.workflow.deps import CaptureQueue, WorkflowDeps, WorkflowError
from app.workflow.discovery import discover
from app.workflow.observability import RunLog
from app.workflow.state import (
    ApplicationCheck,
    ApprovalReason,
    ApprovalState,
    ApprovedCase,
    CaseSummary,
    PlanSummary,
    RequirementSummary,
    WorkflowState,
)
from app.workflow.tools import (
    discovery_tool,
    execution_tool_for_script,
    execution_tool_for_types,
)

MAX_CLARIFICATION_ROUNDS = 3
SCRIPTS_PER_CALL = 4
APPROVAL_VALID_FOR = timedelta(hours=72)
_INSTRUCTION_CHARS = 4000


def _ids(state: WorkflowState) -> tuple[str, str, str]:
    return state["organization_id"], state["project_id"], state["user_id"]


def run_log(state: WorkflowState, deps: WorkflowDeps) -> RunLog:
    instruction = state.get("instruction_id")
    return RunLog(
        deps.session_factory,
        organization_id=uuid.UUID(state["organization_id"]),
        run_id=uuid.UUID(state["run_id"]),
        instruction_id=uuid.UUID(instruction) if instruction else None,
    )


# --- 1 -----------------------------------------------------------------------------------


async def receive_instruction(state: WorkflowState, deps: WorkflowDeps) -> dict[str, Any]:
    """Check that the person may run this, and that what they named exists in the project."""
    async with deps.acting(*_ids(state)) as act:
        act.ctx.require(Permission.EXECUTIONS_RUN)
        act.ctx.require_writable()
        organization = act.ctx.organization_id
        environment = await EnvironmentRepository(act.session, organization).get(
            uuid.UUID(state["environment_id"])
        )
        application = (
            await ApplicationRepository(act.session, organization).get(environment.application_id)
            if environment
            else None
        )
        if (
            environment is None
            or application is None
            or application.project_id != act.ctx.project.id
        ):
            raise WorkflowError("unknown_target", "The environment does not exist in this project.")
        update: dict[str, Any] = {"application_id": str(application.id)}
        if state["mode"] == "run_existing":
            suite = await SuiteRepository(act.session, organization).get_in_project(
                act.ctx.project.id, uuid.UUID(state["suite_id"] or "")
            )
            if suite is None or suite.status != "approved":
                raise WorkflowError("suite_not_approved", "Only an approved suite can be run.")
            update["user_instruction"] = f"Run the approved suite '{suite.name}'."
            return update
        act.ctx.require(Permission.INSTRUCTIONS_CREATE)
        instruction = await InstructionRepository(act.session, organization).get(
            uuid.UUID(state["instruction_id"] or "")
        )
        if instruction is None or instruction.project_id != act.ctx.project.id:
            raise WorkflowError("unknown_instruction", "The instruction does not exist.")
        update["user_instruction"] = instruction.raw_text[:_INSTRUCTION_CHARS]
        return update


# --- 2 -----------------------------------------------------------------------------------


async def analyse_requirement(state: WorkflowState, deps: WorkflowDeps) -> dict[str, Any]:
    """Have the requirement analysed, and summarise where that leaves the instruction."""
    organization = uuid.UUID(state["organization_id"])
    instruction_id = uuid.UUID(state["instruction_id"] or "")
    queue = CaptureQueue()
    token: str | None = None
    async with deps.acting(*_ids(state)) as act:
        instruction = await InstructionRepository(act.session, organization).get_for_update(
            instruction_id
        )
        if instruction is None:
            raise WorkflowError("unknown_instruction", "The instruction does not exist.")
        if instruction.status == "received":
            service = InstructionService(
                session=act.session,
                audit=act.audit,
                jobs=queue,
                gateway=deps.gateway,
                redis=deps.redis,
                settings=deps.settings,
            )
            await service.start_analysis(act.ctx, instruction)
            token = str(queue.take(ANALYSE_JOB)["run_token"])
        elif instruction.status == "analysing":
            # Clarification answers were given, or an earlier attempt was interrupted.
            token = instruction.run_token
    if token is not None:
        await AnalysisRunner(
            session_factory=deps.session_factory, gateway=deps.gateway, settings=deps.settings
        ).run(organization_id=organization, instruction_id=instruction_id, run_token=token)

    async with deps.transaction(state["organization_id"]) as session:
        instruction = await InstructionRepository(session, organization).get(instruction_id)
        assert instruction is not None  # noqa: S101 - it existed a moment ago
        if instruction.status == "failed":
            error = instruction.error or {}
            raise WorkflowError(
                str(error.get("code", "analysis_failed")),
                str(error.get("message", "The requirement could not be analysed.")),
            )
        validation = instruction.validation or {}
        analysis = instruction.analysis or {}
        summary = RequirementSummary(
            status=instruction.status,
            ready=bool(validation.get("ready")),
            feature_name=analysis.get("feature_name"),
            objectives=len(analysis.get("test_objectives", [])),
            open_questions=len(instruction.clarification_questions or []),
            blocking_issues=[
                str(issue.get("code"))
                for issue in validation.get("issues", [])
                if issue.get("severity") == "blocking"
            ],
        )
    update: dict[str, Any] = {"requirement_analysis": summary}
    if summary["status"] != "needs_clarification" and not summary["ready"]:
        update["status"] = "failed"
        update["errors"] = [
            {
                "node": "analyse_requirement",
                "code": "requirement_not_ready",
                "message": "The requirement has blocking issues that questions cannot resolve: "
                + ", ".join(summary["blocking_issues"] or [summary["status"]]),
            }
        ]
    return update


async def request_clarification(state: WorkflowState, deps: WorkflowDeps) -> dict[str, Any]:
    """Stop and wait for a person to answer the open questions."""
    rounds = state.get("clarification_rounds", 0)
    if rounds >= MAX_CLARIFICATION_ROUNDS:
        raise WorkflowError(
            "clarification_limit",
            f"The requirement is still unclear after {rounds} rounds of questions.",
        )
    organization = uuid.UUID(state["organization_id"])
    async with deps.transaction(state["organization_id"]) as session:
        instruction = await InstructionRepository(session, organization).get(
            uuid.UUID(state["instruction_id"] or "")
        )
        questions = list(instruction.clarification_questions or []) if instruction else []
    if not questions:
        # Still incomplete, and nothing left to ask: waiting would never end.
        raise WorkflowError(
            "requirement_incomplete",
            "The requirement is still incomplete and there are no further questions that would "
            "settle it. Rewrite the instruction with the missing details.",
        )
    # Execution stops here. It continues, from the top of this function, when the
    # answers have been stored through the API.
    interrupt({"type": "clarification", "questions": questions})
    return {"clarification_rounds": rounds + 1}


# --- 3 -----------------------------------------------------------------------------------


async def validate_application(state: WorkflowState, deps: WorkflowDeps) -> dict[str, Any]:
    """Check the target is usable and, where possible, look at it read-only through MCP."""
    organization = uuid.UUID(state["organization_id"])
    async with deps.transaction(state["organization_id"]) as session:
        environment = await EnvironmentRepository(session, organization).get(
            uuid.UUID(state["environment_id"])
        )
        if environment is None or environment.status != "active":
            raise WorkflowError("environment_inactive", "The environment is not active.")
        facts = await load_environment_facts(
            session,
            organization_id=organization,
            project_id=uuid.UUID(state["project_id"]),
            environment=environment,
        )
        if not facts.origins:
            raise WorkflowError("no_allowed_origins", "The environment has no allowed origins.")
        allowed = frozenset(environment.allowed_actions)
        base_url = environment.base_url
        viewport = environment.default_viewport
        destinations = DestinationPolicy(
            allowed_origins=facts.origins,
            allow_private_network=environment.allow_private_network,
            allow_http=environment.kind != "production",
        )
        login_paths = []
        for credential in facts.credentials.values():
            try:
                recipe = LoginRecipe.model_validate(list(credential.login_recipe))
            except ValueError:
                continue
            login_paths.append(recipe.root[0].path)  # type: ignore[union-attr]

    choice = discovery_tool(
        browser_available=deps.browser_manager is not None, allowed_actions=allowed
    )
    wanted = state["mode"] == "generate_and_run" and state["options"]["discover_application"]
    if not wanted or choice == "none" or deps.browser_manager is None:
        reason = "not_needed" if not wanted else "unavailable:browser_not_configured"
        return {
            "application": ApplicationCheck(valid=True, inspected_with=reason, pages=[], notes=[])
        }

    found = await discover(
        deps.browser_manager,
        organization_id=organization,
        destinations=destinations,
        allowed_actions=allowed,
        base_url=base_url,
        paths=list(dict.fromkeys(login_paths)),
        options=launch_options(deps.settings, viewport),
        recorder=Recorders(
            LogRecorder(),
            DbRecorder(
                deps.session_factory,
                organization_id=organization,
                thread=state["run_id"],
                node="validate_application",
            ),
        ),
    )
    # The browser service being unavailable is not the application's fault: the run
    # goes on without an inventory. The application not opening is.
    unavailable = found.inspected_with.startswith("unavailable")
    check = ApplicationCheck(
        valid=found.reachable or unavailable,
        inspected_with=found.inspected_with,
        pages=found.pages,
        notes=found.notes,
    )
    update: dict[str, Any] = {"application": check, "mcp_tool_results": found.tool_results}
    if not check["valid"]:
        update["status"] = "failed"
        update["errors"] = [
            {
                "node": "validate_application",
                "code": "application_unreachable",
                "message": "The application could not be opened in a browser: "
                + (", ".join(found.notes) or "no page loaded"),
            }
        ]
    return update


# --- 4 -----------------------------------------------------------------------------------


async def generate_test_plan(state: WorkflowState, deps: WorkflowDeps) -> dict[str, Any]:
    """Derive the plan from the analysed requirement. It awaits approval with the cases."""
    organization = uuid.UUID(state["organization_id"])
    async with deps.acting(*_ids(state)) as act:
        instruction = await InstructionRepository(act.session, organization).get_for_update(
            uuid.UUID(state["instruction_id"] or "")
        )
        if instruction is None or instruction.analysis is None:
            raise WorkflowError("requirement_not_ready", "There is no analysed requirement.")
        analysis = RequirementAnalysis.model_validate(instruction.analysis)
        _, _, facts, _ = await load_target(act.session, instruction)
        report = apply_validation(instruction, analysis, facts)
        if not report.ready:
            raise WorkflowError(
                "requirement_not_ready", "The requirement has open questions or blocking issues."
            )
        execution_tool = execution_tool_for_types(list(instruction.test_types))
        application = state.get("application")
        inspected = application["inspected_with"] if application else ""
        selection = {objective.id: execution_tool for objective in analysis.test_objectives}
        plans = PlanRepository(act.session, organization)
        plan = await plans.latest(instruction.id)
        if plan is None or plan.status != "awaiting_approval":
            await plans.supersede_all(instruction.id)
            plan = await plans.add(
                TestPlan(
                    organization_id=organization,
                    instruction_id=instruction.id,
                    version=(plan.version + 1) if plan else 1,
                    # Not approved yet: a person approves it together with the cases.
                    status="awaiting_approval",
                    summary=analysis.summary,
                    scope=plan_scope(
                        analysis, [i.message for i in report.issues if i.severity == "warning"]
                    ),
                    strategy={
                        "execution_tool": execution_tool,
                        "discovery_tool": "mcp_read_only"
                        if inspected == "mcp_read_only"
                        else "none",
                        "tool_selection": selection,
                    },
                    model=instruction.analysis_model,
                )
            )
        return {
            "test_plan": PlanSummary(
                plan_id=str(plan.id),
                version=plan.version,
                objectives=len(analysis.test_objectives),
                tool_selection=selection,
                discovery="mcp_read_only" if inspected == "mcp_read_only" else "none",
            )
        }


# --- 5 -----------------------------------------------------------------------------------


async def generate_test_cases(state: WorkflowState, deps: WorkflowDeps) -> dict[str, Any]:
    """Write the cases (once), then the executable script of each case that needs one."""
    organization = uuid.UUID(state["organization_id"])
    project = uuid.UUID(state["project_id"])
    instruction_id = uuid.UUID(state["instruction_id"] or "")
    again = bool(state.get("generated_test_cases"))
    log = run_log(state, deps)
    errors: list[dict[str, str]] = []

    if not again:
        token: str | None = None
        async with deps.acting(*_ids(state)) as act:
            act.ctx.require(Permission.CASES_MANAGE)
            instruction = await InstructionRepository(act.session, organization).get_for_update(
                instruction_id
            )
            assert instruction is not None  # noqa: S101
            if instruction.status == "analysed":
                await require_budget(
                    act.session, organization, deps.settings, act.ctx.org.organization.settings
                )
                token = begin_generation(instruction)
            elif instruction.status == "generating":
                token = instruction.run_token  # an earlier attempt was interrupted
        if token is not None:
            await GenerationRunner(
                session_factory=deps.session_factory, gateway=deps.gateway, settings=deps.settings
            ).run(
                organization_id=organization,
                instruction_id=instruction_id,
                run_token=token,
                case_ids=[],
                feedback=None,
            )
        async with deps.transaction(state["organization_id"]) as session:
            instruction = await InstructionRepository(session, organization).get(instruction_id)
            generation = (instruction.generation or {}) if instruction else {}
            if generation.get("state") == "failed":
                error = generation.get("error") or {}
                raise WorkflowError(
                    str(error.get("code", "generation_failed")),
                    str(error.get("message", "No test cases could be generated.")),
                )

    # Which cases need a script: none yet, or one that did not pass the checks.
    previous = state.get("script_problems", {})
    async with deps.transaction(state["organization_id"]) as session:
        environment = await EnvironmentRepository(session, organization).get(
            uuid.UUID(state["environment_id"])
        )
        assert environment is not None  # noqa: S101 - checked by validate_application
        facts = await load_environment_facts(
            session, organization_id=organization, project_id=project, environment=environment
        )
        cases = await TestCaseRepository(session, organization).list_in_project(
            project, instruction_id=instruction_id
        )
        targets: list[CaseToScript] = []
        for view in await load_views(session, organization, cases):
            issues = (view.version.validation or {}).get("issues", [])
            script_issues = [
                str(i.get("message"))
                for i in issues
                if str(i.get("code", "")).startswith("automation_")
                and i.get("severity") == "blocking"
            ]
            if view.version.automation is None or script_issues or view.case.key in previous:
                targets.append(
                    CaseToScript(
                        view.case.key,
                        view.content,
                        [*script_issues, *previous.get(view.case.key, [])],
                    )
                )
        context = ScriptContext(
            base_url=environment.base_url,
            read_only=environment.is_read_only,
            allowed_actions=sorted(environment.allowed_actions),
            available_credential_roles=sorted(
                role
                for role, credential in facts.credentials.items()
                if credential.source == "stored" and credential.login_recipe
            ),
            environment_variables=sorted(facts.variables),
            available_files=sorted(facts.file_names),
        )

    inspected = state.get("application")
    pages = [dict(page) for page in (inspected["pages"] if inspected else [])]
    written: dict[str, Any] = {}
    for start in range(0, len(targets), SCRIPTS_PER_CALL):
        batch = targets[start : start + SCRIPTS_PER_CALL]
        try:
            result = await generate_scripts(
                deps.gateway,
                context=context,
                pages=pages,
                cases=batch,
                recorder=log.llm_recorder("generate_test_cases"),
            )
        except LLMError as exc:
            errors.append({"node": "generate_test_cases", "code": exc.code, "message": str(exc)})
            break
        wanted = {case.key for case in batch}
        written.update({s.case_key: s for s in result.output.scripts if s.case_key in wanted})

    problems: dict[str, list[str]] = {}
    async with deps.transaction(state["organization_id"]) as session:
        environment = await EnvironmentRepository(session, organization).get(
            uuid.UUID(state["environment_id"])
        )
        assert environment is not None  # noqa: S101
        facts = await load_environment_facts(
            session, organization_id=organization, project_id=project, environment=environment
        )
        cases = await TestCaseRepository(session, organization).list_in_project(
            project, instruction_id=instruction_id
        )
        for view in await load_views(session, organization, cases):
            generated = written.get(view.case.key)
            if generated is None:
                continue
            try:
                script = to_script(generated)
            except ScriptConversionError as exc:
                # Not stored. The reason goes back to the model if there is another round.
                problems[view.case.key] = [f"The script was not valid: {exc}"]
                continue
            report = validate_case(
                view.content,
                await build_facts(
                    session,
                    organization_id=organization,
                    project_id=project,
                    environment_id=view.case.environment_id,
                    instruction_id=instruction_id,
                    exclude_case_id=view.case.id,
                ),
            )
            report.issues.extend(check_script(script, facts.script_facts(script, view.content)))
            report.issues.extend(
                CaseIssue(
                    "automation_unresolved",
                    "warning",
                    f"The script's author could not find this in the application: {item[:200]}",
                )
                for item in generated.unresolved[:10]
            )
            await write_version(
                session,
                view.case,
                view.content,
                report,
                source="ai",
                change_reason="regenerated",
                created_by=None,
                automation=script.model_dump(mode="json", exclude_none=True),
            )
    update: dict[str, Any] = {
        "regeneration_count": state.get("regeneration_count", 0) + (1 if again else 0),
        "script_problems": problems,
    }
    if errors:
        update["errors"] = errors
    return update


# --- 6 -----------------------------------------------------------------------------------


async def validate_test_cases(state: WorkflowState, deps: WorkflowDeps) -> dict[str, Any]:
    """Check every case and script with the deterministic checks. No model."""
    organization = uuid.UUID(state["organization_id"])
    project = uuid.UUID(state["project_id"])
    instruction_id = uuid.UUID(state["instruction_id"] or "")
    problems = state.get("script_problems", {})
    summaries: list[CaseSummary] = []
    async with deps.transaction(state["organization_id"]) as session:
        environment = await EnvironmentRepository(session, organization).get(
            uuid.UUID(state["environment_id"])
        )
        assert environment is not None  # noqa: S101
        facts = await load_environment_facts(
            session, organization_id=organization, project_id=project, environment=environment
        )
        cases = await TestCaseRepository(session, organization).list_in_project(
            project, instruction_id=instruction_id
        )
        for view in await load_views(session, organization, cases):
            report = validate_case(
                view.content,
                await build_facts(
                    session,
                    organization_id=organization,
                    project_id=project,
                    environment_id=view.case.environment_id,
                    instruction_id=instruction_id,
                    exclude_case_id=view.case.id,
                ),
            )
            script, issues = parse_script(view.version.automation)
            if script is not None:
                issues = check_script(script, facts.script_facts(script, view.content))
            kept = [
                CaseIssue(i["code"], i["severity"], i["message"], i.get("step"))
                for i in (view.version.validation or {}).get("issues", [])
                if i.get("code") == "automation_unresolved"
            ]
            report.issues.extend([*issues, *kept])
            view.version.validation = report.as_dict()
            blocking = [i.code for i in report.issues if i.severity == "blocking"]
            if view.version.automation is None:
                blocking.append("no_script")
            if view.case.key in problems:
                blocking.append("automation_invalid")
            summaries.append(
                CaseSummary(
                    case_id=str(view.case.id),
                    key=view.case.key,
                    title=view.content.title,
                    version=view.version.version,
                    ready=not blocking,
                    has_script=script is not None,
                    tool=execution_tool_for_script(script),
                    blocking_issues=sorted(set(blocking)),
                )
            )
    return {"generated_test_cases": summaries}


# --- 7 -----------------------------------------------------------------------------------


async def approval_checkpoint(state: WorkflowState, deps: WorkflowDeps) -> dict[str, Any]:
    """Work out whether a person has to approve, wait for them if so, then act on the decision."""
    organization = uuid.UUID(state["organization_id"])
    project = uuid.UUID(state["project_id"])
    run_id = uuid.UUID(state["run_id"])
    existing = state["mode"] == "run_existing"

    reasons: list[ApprovalReason] = [] if existing else ["generated_test_cases"]
    pinned: list[ApprovedCase] = []
    async with deps.transaction(state["organization_id"]) as session:
        environment = await EnvironmentRepository(session, organization).get(
            uuid.UUID(state["environment_id"])
        )
        assert environment is not None  # noqa: S101
        cases_repository = TestCaseRepository(session, organization)
        if existing:
            suites = SuiteRepository(session, organization)
            members = (await suites.members([uuid.UUID(state["suite_id"] or "")])).popitem()[1]
            cases = []
            for member in members:
                case = await cases_repository.get(member.test_case_id)
                if case is not None and member.approved_version_id is not None:
                    cases.append(case)
                    pinned.append(
                        ApprovedCase(
                            case_id=str(case.id),
                            key=case.key,
                            version_id=str(member.approved_version_id),
                        )
                    )
        else:
            cases = list(
                await cases_repository.list_in_project(
                    project, instruction_id=uuid.UUID(state["instruction_id"] or "")
                )
            )
        views = await load_views(session, organization, cases)
        scripts = [parse_script(view.version.automation)[0] for view in views]
        changing = any(
            script is not None
            and (script.login_as is not None or any(changes_state(s) for s in script.steps))
            for script in scripts
        )
        if changing and environment.requires_approval:
            reasons.append("state_changing_operations")
        if any(
            script is not None and any(changes_state(s) for s in script.cleanup)
            for script in scripts
        ):
            reasons.append("destructive_cleanup")
        if not existing and any(view.content.objective_id is None for view in views):
            reasons.append("outside_default_scope")
        needs_admin = "state_changing_operations" in reasons or environment.kind == "production"

        if not reasons:
            return {
                "approval": ApprovalState(
                    required=False,
                    reasons=[],
                    approval_id=None,
                    decision=None,
                    decided_by=None,
                    note=None,
                ),
                "approved_test_cases": pinned,
            }

        approvals = ApprovalRepository(session, organization)
        approval = await approvals.latest_for(run_id)
        if approval is None:
            approval = await approvals.add(
                Approval(
                    organization_id=organization,
                    subject_type="test_cases",
                    subject_id=run_id,
                    reason_code="mutating_steps" if changing else "policy",
                    payload={"reasons": reasons, "needs_admin": needs_admin},
                    requested_by=uuid.UUID(state["user_id"]),
                    expires_at=datetime.now(UTC) + APPROVAL_VALID_FOR,
                )
            )
        approval_id = str(approval.id)
        summary = [
            {
                "case_key": c["key"],
                "title": c["title"],
                "ready": c["ready"],
                "has_script": c["has_script"],
                "tool": c["tool"],
                "blocking_issues": c["blocking_issues"],
            }
            for c in state.get("generated_test_cases", [])
        ]

    # Execution stops here until someone decides. Nothing below runs before that.
    decision = interrupt(
        {
            "type": "approval",
            "approval_id": approval_id,
            "reasons": reasons,
            "needs_admin": needs_admin,
            "cases": summary or [{"case_key": c["key"]} for c in pinned],
        }
    )
    verdict = str(decision.get("decision"))
    decided_by = str(decision.get("decided_by"))
    result = ApprovalState(
        required=True,
        reasons=reasons,
        approval_id=approval_id,
        decision="approved" if verdict == "approved" else "rejected",
        decided_by=decided_by,
        note=decision.get("note"),
    )
    if verdict != "approved":
        async with deps.transaction(state["organization_id"]) as session:
            if not existing:
                instruction = await InstructionRepository(session, organization).get_for_update(
                    uuid.UUID(state["instruction_id"] or "")
                )
                if instruction is not None:
                    instruction.status = "rejected"
                plan = await session.get(TestPlan, uuid.UUID(state["test_plan"]["plan_id"]))  # type: ignore[index]
                if plan is not None:
                    plan.status = "rejected"
        return {"approval": result, "status": "rejected"}

    if existing:
        return {"approval": result, "approved_test_cases": pinned}

    # Approve as the person who decided, with their permissions checked again here.
    approved: list[ApprovedCase] = []
    excluded: list[CaseSummary] = []
    async with deps.acting(state["organization_id"], state["project_id"], decided_by) as act:
        act.ctx.require(Permission.CASES_APPROVE)
        if needs_admin:
            act.ctx.require(Permission.ENVIRONMENTS_SECURITY)
        cases = list(
            await TestCaseRepository(act.session, organization).list_in_project(
                project, instruction_id=uuid.UUID(state["instruction_id"] or "")
            )
        )
        known = {c["case_id"]: c for c in state.get("generated_test_cases", [])}
        for view in await load_views(act.session, organization, cases):
            summary_of = known.get(str(view.case.id)) or CaseSummary(
                case_id=str(view.case.id), key=view.case.key, title=view.content.title,
                version=view.version.version, ready=False, has_script=False, tool="none",
                blocking_issues=["added_after_validation"],
            )  # fmt: skip
            if view.version.automation is None:
                excluded.append({**summary_of, "blocking_issues": ["no_script"]})
                continue
            try:
                # A savepoint: a case that is not ready leaves the others approved.
                async with act.session.begin_nested():
                    await approve_case(act.session, act.ctx, view.case)
            except ConflictError as exc:
                codes = sorted({error.code for error in exc.errors}) or ["case_not_ready"]
                excluded.append({**summary_of, "blocking_issues": codes})
                continue
            approved.append(
                ApprovedCase(
                    case_id=str(view.case.id),
                    key=view.case.key,
                    version_id=str(view.case.approved_version_id),
                )
            )
        plan = await act.session.get(TestPlan, uuid.UUID(state["test_plan"]["plan_id"]))  # type: ignore[index]
        if plan is not None:
            plan.status = "approved"
            plan.approved_by = act.ctx.user.id
            plan.approved_at = datetime.now(UTC)
    update: dict[str, Any] = {
        "approval": result,
        "approved_test_cases": approved,
        "excluded_test_cases": excluded,
    }
    if not approved:
        update["status"] = "failed"
        update["errors"] = [
            {
                "node": "approval_checkpoint",
                "code": "nothing_to_run",
                "message": "No generated case is ready to run: each is missing a script or has "
                "blocking issues.",
            }
        ]
    return update


async def touch_run(deps: WorkflowDeps, state: WorkflowState, **fields: Any) -> WorkflowRun | None:
    """Update the run's own row."""
    async with deps.transaction(state["organization_id"]) as session:
        run = await session.get(WorkflowRun, uuid.UUID(state["run_id"]))
        if run is not None:
            for name, value in fields.items():
                setattr(run, name, value)
        return run
