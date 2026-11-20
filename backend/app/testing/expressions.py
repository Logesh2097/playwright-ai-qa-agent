"""Business-rule expressions: a small, safe arithmetic and comparison language.

    points == floor(bill_amount / 10)
    total == subtotal + tax and total <= 5000

An expression is parsed into a syntax tree and evaluated by the code below.
Only the listed operators and functions exist; there is no attribute access to
objects, no indexing, no calls to anything else and no exponentiation. Nothing
is ever passed to `eval`.

Numbers are decimals, not binary floats: `0.1 + 0.2 == 0.3` is true, which is
what a rule about money needs.
"""

import ast
import math
import operator
import re
from collections.abc import Callable, Mapping
from decimal import Decimal, DivisionByZero, InvalidOperation
from typing import Any

MAX_LENGTH = 500
_MAX_NODES = 200
NAMESPACES = ("var", "data", "env")
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")


class ExpressionError(ValueError):
    """The expression is not valid, or could not be evaluated with the given values."""


def to_number(value: Any) -> Decimal:
    """Read a number the way a person would: "₹1,250.50" is 1250.50.

    Text must contain exactly one number; "3 of 10" is refused rather than guessed at.
    """
    if isinstance(value, bool):
        raise ExpressionError("a true/false value is not a number")
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, str):
        found = _NUMBER.findall(value.replace(",", ""))
        if len(found) == 1:
            return Decimal(found[0])
        raise ExpressionError(f"{value!r} does not contain exactly one number")
    raise ExpressionError(f"{type(value).__name__} is not a number")


def _floor(value: Any) -> int:
    return math.floor(to_number(value))


def _ceil(value: Any) -> int:
    return math.ceil(to_number(value))


def _round(value: Any, digits: Any = 0) -> Decimal:
    """Round half up, as people expect for money (not banker's rounding)."""
    places = int(to_number(digits))
    if not 0 <= places <= 6:
        raise ExpressionError("round() takes 0 to 6 decimal places")
    return to_number(value).quantize(Decimal(1).scaleb(-places), rounding="ROUND_HALF_UP")


_FUNCTIONS: dict[str, Callable[..., Any]] = {
    "floor": _floor,
    "ceil": _ceil,
    "round": _round,
    "abs": lambda value: abs(to_number(value)),
    "min": lambda *values: min(to_number(v) for v in values),
    "max": lambda *values: max(to_number(v) for v in values),
    "number": to_number,
    "text": lambda value: str(value),
    "lower": lambda value: str(value).lower(),
    "upper": lambda value: str(value).upper(),
    "strip": lambda value: str(value).strip(),
    "length": lambda value: len(str(value)),
}
_ARITHMETIC: dict[type[ast.operator], Callable[[Decimal, Decimal], Decimal]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
}
_COMPARISONS: dict[type[ast.cmpop], Callable[[Any, Any], bool]] = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
}
_ALLOWED = (
    ast.Expression, ast.BoolOp, ast.And, ast.Or, ast.UnaryOp, ast.Not, ast.USub, ast.UAdd,
    ast.BinOp, ast.Compare, ast.Constant, ast.Name, ast.Attribute, ast.Call, ast.Load, ast.IfExp,
    ast.In, ast.NotIn, *_ARITHMETIC, *_COMPARISONS,
)  # fmt: skip


def parse(expression: str) -> ast.Expression:
    """Check that an expression uses only what the language allows."""
    if not expression.strip():
        raise ExpressionError("the expression is empty")
    if len(expression) > MAX_LENGTH:
        raise ExpressionError(f"the expression is longer than {MAX_LENGTH} characters")
    try:
        tree = ast.parse(expression.strip(), mode="eval")
    except SyntaxError as exc:
        raise ExpressionError(f"not a valid expression: {exc.msg}") from None
    nodes = list(ast.walk(tree))
    if len(nodes) > _MAX_NODES:
        raise ExpressionError("the expression is too complex")
    for node in nodes:
        if not isinstance(node, _ALLOWED):
            raise ExpressionError(f"{type(node).__name__} is not allowed in an expression")
        if isinstance(node, ast.Constant) and not isinstance(node.value, int | float | str | bool):
            raise ExpressionError("only numbers, text and true/false can be written literally")
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in _FUNCTIONS:
                raise ExpressionError(
                    "only these functions exist: " + ", ".join(sorted(_FUNCTIONS))
                )
            if node.keywords:
                raise ExpressionError("functions take plain arguments only")
        if isinstance(node, ast.Attribute) and not (
            isinstance(node.value, ast.Name) and node.value.id in NAMESPACES
        ):
            raise ExpressionError("a dot may only follow var, data or env")
    return tree


def names_used(expression: str) -> set[str]:
    """Values the expression reads, as `var.total`, `data.amount`; a bare name is a variable."""
    tree = parse(expression)
    functions = {
        n.func.id
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    qualified = {
        f"{n.value.id}.{n.attr}"
        for n in ast.walk(tree)
        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
    }
    bare = {
        n.id
        for n in ast.walk(tree)
        if isinstance(n, ast.Name) and n.id not in functions and n.id not in NAMESPACES
        and n.id not in ("true", "false")
    }  # fmt: skip
    return qualified | {f"var.{name}" for name in bare}


def evaluate(expression: str, values: Mapping[str, Any]) -> Any:
    """Evaluate with `values` keyed like `names_used` returns them."""
    tree = parse(expression)
    try:
        return _eval(tree.body, values)
    except (DivisionByZero, ZeroDivisionError):
        raise ExpressionError("division by zero") from None
    except InvalidOperation:
        raise ExpressionError("the calculation is not defined for these values") from None
    except TypeError:
        raise ExpressionError("these values cannot be compared or combined") from None


def _lookup(name: str, values: Mapping[str, Any]) -> Any:
    if name not in values:
        raise ExpressionError(f"{name} has no value")
    return values[name]


def _eval(node: ast.AST, values: Mapping[str, Any]) -> Any:
    if isinstance(node, ast.Constant):
        return (
            to_number(node.value)
            if isinstance(node.value, int | float) and not isinstance(node.value, bool)
            else node.value
        )
    if isinstance(node, ast.Name):
        if node.id in ("true", "false"):
            return node.id == "true"
        return _lookup(f"var.{node.id}", values)
    if isinstance(node, ast.Attribute):
        assert isinstance(node.value, ast.Name)  # noqa: S101 - checked by parse
        return _lookup(f"{node.value.id}.{node.attr}", values)
    if isinstance(node, ast.UnaryOp):
        operand = _eval(node.operand, values)
        if isinstance(node.op, ast.Not):
            return not operand
        return -to_number(operand) if isinstance(node.op, ast.USub) else to_number(operand)
    if isinstance(node, ast.BinOp):
        left, right = _eval(node.left, values), _eval(node.right, values)
        if isinstance(node.op, ast.Add) and isinstance(left, str) and isinstance(right, str):
            return left + right
        return _ARITHMETIC[type(node.op)](to_number(left), to_number(right))
    if isinstance(node, ast.BoolOp):
        results = [bool(_eval(value, values)) for value in node.values]
        return all(results) if isinstance(node.op, ast.And) else any(results)
    if isinstance(node, ast.IfExp):
        return _eval(node.body if _eval(node.test, values) else node.orelse, values)
    if isinstance(node, ast.Compare):
        left = _eval(node.left, values)
        for op, comparator in zip(node.ops, node.comparators, strict=True):
            right = _eval(comparator, values)
            if isinstance(op, ast.In | ast.NotIn):
                found = str(left) in str(right)
                ok = found if isinstance(op, ast.In) else not found
            else:
                ok = _COMPARISONS[type(op)](*_comparable(left, right))
            if not ok:
                return False
            left = right
        return True
    if isinstance(node, ast.Call):
        assert isinstance(node.func, ast.Name)  # noqa: S101 - checked by parse
        return _FUNCTIONS[node.func.id](*[_eval(arg, values) for arg in node.args])
    raise ExpressionError(f"{type(node).__name__} is not allowed in an expression")


def _comparable(left: Any, right: Any) -> tuple[Any, Any]:
    """A number compared with text that holds a number compares as numbers."""
    numeric = (int, float, Decimal)
    if (isinstance(left, numeric) and not isinstance(left, bool)) or (
        isinstance(right, numeric) and not isinstance(right, bool)
    ):
        if isinstance(left, bool) or isinstance(right, bool):
            return left, right
        return to_number(left), to_number(right)
    return left, right
