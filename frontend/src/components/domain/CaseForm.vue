<script setup lang="ts">
import { reactive, ref } from 'vue'

import BaseButton from '@/components/ui/BaseButton.vue'
import FormField from '@/components/ui/FormField.vue'
import { useFormErrors } from '@/composables/useFormErrors'
import type { CaseCategory, CaseContent, CaseStep, Priority, Severity } from '@/types/api'

const props = defineProps<{
  /** Present when editing; absent when writing a new case by hand. */
  content?: CaseContent
  /** Objectives of the approved plan, to link the case to one. */
  objectives?: { id: string; title: string }[]
  save: (content: CaseContent) => Promise<void>
}>()
const emit = defineEmits<{ cancel: [] }>()

const CATEGORIES: Record<CaseCategory, string> = {
  positive: 'Positive',
  negative: 'Negative',
  boundary: 'Boundary',
  edge_case: 'Edge case',
  form_validation: 'Form validation',
  navigation: 'Navigation',
  authentication: 'Authentication',
  api_validation: 'API validation',
  responsive: 'Responsive UI',
  business_logic: 'Business logic',
}
const PRIORITIES: Priority[] = ['p0', 'p1', 'p2', 'p3']
const SEVERITIES: Severity[] = ['critical', 'major', 'minor', 'trivial']
const ACTIONS = [
  'navigate',
  'click',
  'fill',
  'select',
  'check',
  'uncheck',
  'press',
  'hover',
  'upload',
  'wait_for',
  'set_viewport',
  'api_request',
  'assert_visible',
  'assert_hidden',
  'assert_text',
  'assert_value',
  'assert_url',
  'assert_enabled',
  'assert_disabled',
  'assert_status',
  'assert_json',
  'observe',
]

const blankStep = (): CaseStep => ({
  action: 'click',
  description: '',
  target: '',
  value: '',
  side_effect: 'none',
})

const source = props.content
const form = reactive({
  title: source?.title ?? '',
  description: source?.description ?? '',
  category: source?.category ?? ('positive' as CaseCategory),
  test_type: source?.test_type ?? ('ui' as const),
  objective_id: source?.objective_id ?? '',
  priority: source?.priority ?? ('p2' as Priority),
  severity: source?.severity ?? ('minor' as Severity),
  preconditions: (source?.preconditions ?? []).join('\n'),
  tags: (source?.tags ?? []).join(', '),
  dependencies: (source?.dependencies ?? []).join('\n'),
  assumptions: (source?.assumptions ?? []).join('\n'),
  expected_result: source?.expected_result ?? '',
  automation_feasibility: source?.automation_feasibility ?? ('automatable' as const),
  automation_notes: source?.automation_notes ?? '',
  verification_method: source?.verification.method ?? 'ui_assertion',
  verification_description: source?.verification.description ?? '',
  // NAME=value per line
  test_data: (source?.test_data ?? []).map((d) => `${d.name}=${d.value ?? ''}`).join('\n'),
})
const steps = ref<CaseStep[]>(
  source ? source.steps.map((step) => ({ ...step })) : [{ ...blankStep(), action: 'navigate' }],
)

const lines = (text: string) =>
  text
    .split('\n')
    .map((line) => line.trim())
    .filter(Boolean)

function move(index: number, delta: number): void {
  const target = index + delta
  if (target < 0 || target >= steps.value.length) return
  const copy = [...steps.value]
  const [step] = copy.splice(index, 1)
  if (step) copy.splice(target, 0, step)
  steps.value = copy
}

const errors = useFormErrors()
const saving = ref(false)

async function submit(): Promise<void> {
  saving.value = true
  errors.clear()
  const expected = form.expected_result.trim()
  const content: CaseContent = {
    title: form.title,
    description: form.description || null,
    category: form.category,
    test_type: form.test_type,
    objective_id: form.objective_id || null,
    preconditions: lines(form.preconditions),
    test_data: lines(form.test_data).map((line) => {
      const at = line.indexOf('=')
      const name = at === -1 ? line : line.slice(0, at).trim()
      const value = at === -1 ? '' : line.slice(at + 1).trim()
      return { name, value: value === '' && at === -1 ? null : value, description: null }
    }),
    steps: steps.value.map((step) => ({
      ...step,
      target: step.target?.trim() || null,
      value: step.value === '' || step.value === null ? null : step.value,
    })),
    // An empty result is "unknown". The server labels a result typed here as the
    // reviewer's own; a result can never be claimed to come from somewhere else.
    expected_result: expected || null,
    expected_source: expected
      ? source && source.expected_result === expected && source.expected_source !== 'unknown'
        ? source.expected_source
        : 'human_specified'
      : 'unknown',
    priority: form.priority,
    severity: form.severity,
    tags: form.tags
      .split(',')
      .map((tag) => tag.trim())
      .filter(Boolean),
    dependencies: lines(form.dependencies),
    automation_feasibility: form.automation_feasibility,
    automation_notes: form.automation_notes || null,
    verification: {
      method: form.verification_method,
      description: form.verification_description,
    },
    assumptions: lines(form.assumptions),
  }
  try {
    await props.save(content)
  } catch (error) {
    errors.capture(error)
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <form class="stack" novalidate @submit.prevent="submit">
    <p v-if="errors.general.value" class="notice notice--danger" role="alert">
      {{ errors.general.value }}
    </p>

    <FormField v-slot="field" label="Title" :error="errors.fields.title">
      <input :id="field.id" v-model.trim="form.title" class="input" maxlength="200" required />
    </FormField>
    <FormField v-slot="field" label="Description">
      <textarea :id="field.id" v-model.trim="form.description" class="input" rows="2" />
    </FormField>

    <div class="form-grid">
      <FormField v-slot="field" label="Category">
        <select :id="field.id" v-model="form.category" class="input">
          <option v-for="(label, value) in CATEGORIES" :key="value" :value="value">
            {{ label }}
          </option>
        </select>
      </FormField>
      <FormField v-slot="field" label="Type">
        <select :id="field.id" v-model="form.test_type" class="input">
          <option value="ui">UI</option>
          <option value="api">API</option>
          <option value="responsive">Responsive</option>
        </select>
      </FormField>
      <FormField v-slot="field" label="Priority">
        <select :id="field.id" v-model="form.priority" class="input">
          <option v-for="priority in PRIORITIES" :key="priority" :value="priority">
            {{ priority.toUpperCase() }}
          </option>
        </select>
      </FormField>
      <FormField v-slot="field" label="Severity">
        <select :id="field.id" v-model="form.severity" class="input">
          <option v-for="severity in SEVERITIES" :key="severity" :value="severity">
            {{ severity }}
          </option>
        </select>
      </FormField>
      <FormField v-if="objectives && objectives.length > 0" v-slot="field" label="Verifies">
        <select :id="field.id" v-model="form.objective_id" class="input">
          <option value="">Not part of the requirement</option>
          <option v-for="objective in objectives" :key="objective.id" :value="objective.id">
            {{ objective.id }} — {{ objective.title }}
          </option>
        </select>
      </FormField>
    </div>

    <FormField
      v-slot="field"
      label="Preconditions"
      hint="One per line. What must be true before the first step."
    >
      <textarea :id="field.id" v-model="form.preconditions" class="input" rows="2" />
    </FormField>
    <FormField
      v-slot="field"
      label="Test data"
      :error="errors.fields.test_data"
      hint="One per line: name=value. Steps refer to a value as {{data.name}}."
    >
      <textarea :id="field.id" v-model="form.test_data" class="input mono" rows="2" />
    </FormField>

    <fieldset class="fieldset stack">
      <legend>Steps</legend>
      <p class="small muted">
        Describe each element the way you would point at it ("the Email field"). For account details
        write <span class="mono" v-text="'{{cred.customer.password}}'" />, never a real password.
      </p>
      <ol class="steps">
        <li v-for="(step, index) in steps" :key="index" class="step">
          <div class="step__row">
            <label>
              <span class="visually-hidden">Action of step {{ index + 1 }}</span>
              <select v-model="step.action" class="input">
                <option v-for="action in ACTIONS" :key="action" :value="action">
                  {{ action.replace('_', ' ') }}
                </option>
              </select>
            </label>
            <label class="grow">
              <span class="visually-hidden">Description of step {{ index + 1 }}</span>
              <input
                v-model.trim="step.description"
                class="input"
                placeholder="What happens in this step"
              />
            </label>
          </div>
          <div class="step__row">
            <label class="grow">
              <span class="visually-hidden">Element of step {{ index + 1 }}</span>
              <input
                v-model="step.target"
                class="input"
                placeholder="Element, e.g. the Save button"
              />
            </label>
            <label class="grow">
              <span class="visually-hidden">Value of step {{ index + 1 }}</span>
              <input
                v-model="step.value"
                class="input mono"
                placeholder="Value, path or expected text"
              />
            </label>
            <label>
              <span class="visually-hidden">Effect of step {{ index + 1 }} on data</span>
              <select v-model="step.side_effect" class="input">
                <option value="none">changes nothing</option>
                <option value="create">creates data</option>
                <option value="update">changes data</option>
                <option value="delete">deletes data</option>
                <option value="external">external effect</option>
              </select>
            </label>
          </div>
          <div class="cluster">
            <BaseButton variant="ghost" :disabled="index === 0" @click="move(index, -1)">
              Move up
            </BaseButton>
            <BaseButton
              variant="ghost"
              :disabled="index === steps.length - 1"
              @click="move(index, 1)"
            >
              Move down
            </BaseButton>
            <BaseButton variant="ghost" @click="steps.splice(index, 1)">Remove step</BaseButton>
          </div>
        </li>
      </ol>
      <div>
        <BaseButton variant="secondary" @click="steps.push(blankStep())">Add step</BaseButton>
      </div>
    </fieldset>

    <FormField
      v-slot="field"
      label="Expected result"
      :error="errors.fields.expected_result"
      hint="What must be observed for the case to pass. Leave empty if it is not known yet; the case then cannot be approved."
    >
      <textarea :id="field.id" v-model="form.expected_result" class="input" rows="2" />
    </FormField>

    <div class="form-grid">
      <FormField v-slot="field" label="How it is verified">
        <select :id="field.id" v-model="form.verification_method" class="input">
          <option value="ui_assertion">Check on the page</option>
          <option value="api_response">API response</option>
          <option value="data_check">Data check</option>
          <option value="visual_check">Visual check</option>
          <option value="manual_review">Manual review</option>
        </select>
      </FormField>
      <FormField v-slot="field" label="Automation">
        <select :id="field.id" v-model="form.automation_feasibility" class="input">
          <option value="automatable">Can be automated</option>
          <option value="partially_automatable">Partly</option>
          <option value="manual_only">Manual only</option>
        </select>
      </FormField>
    </div>
    <FormField v-slot="field" label="Verification strategy" hint="How a pass is told from a fail.">
      <input :id="field.id" v-model.trim="form.verification_description" class="input" />
    </FormField>
    <FormField
      v-if="form.automation_feasibility !== 'automatable'"
      v-slot="field"
      label="Why not fully automatable"
    >
      <input :id="field.id" v-model.trim="form.automation_notes" class="input" />
    </FormField>

    <div class="form-grid">
      <FormField v-slot="field" label="Tags" hint="Comma-separated">
        <input :id="field.id" v-model="form.tags" class="input" />
      </FormField>
      <FormField v-slot="field" label="Depends on" hint="One per line">
        <textarea :id="field.id" v-model="form.dependencies" class="input" rows="1" />
      </FormField>
    </div>
    <FormField
      v-if="form.assumptions"
      v-slot="field"
      label="Unconfirmed assumptions"
      hint="The case cannot be approved while anything is listed here. If an assumption is true, move it to the preconditions; otherwise delete it."
    >
      <textarea :id="field.id" v-model="form.assumptions" class="input" rows="2" />
    </FormField>

    <div class="cluster">
      <BaseButton type="submit" :loading="saving">
        {{ source ? 'Save as new version' : 'Add test case' }}
      </BaseButton>
      <BaseButton variant="ghost" @click="emit('cancel')">Cancel</BaseButton>
    </div>
  </form>
</template>

<style scoped>
.fieldset {
  margin: 0;
  padding: var(--space-4);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
}

.fieldset legend {
  padding: 0 var(--space-2);
  font-size: var(--text-sm);
  font-weight: 600;
}

.steps {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
  margin: 0;
  padding-left: var(--space-5);
}

.step {
  padding-left: var(--space-1);
}

.step__row {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2);
  margin-bottom: var(--space-2);
}

.step__row .input {
  min-width: 9rem;
}
</style>
