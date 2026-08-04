"""A small, deterministic arithmetic tool without ``eval``."""

from __future__ import annotations

import ast
import math
import operator
from collections.abc import Mapping
from typing import Any

from ..registry import Tool, ToolExecutionContext


_BINARY = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}


def _evaluate(node: ast.AST, *, depth: int = 0) -> int | float:
    if depth > 32:
        raise ValueError("Expression is too deeply nested")
    if isinstance(node, ast.Expression):
        return _evaluate(node.body, depth=depth + 1)
    if isinstance(node, ast.Constant) and type(node.value) in (int, float):
        value = node.value
    elif isinstance(node, ast.BinOp) and type(node.op) in _BINARY:
        left = _evaluate(node.left, depth=depth + 1)
        right = _evaluate(node.right, depth=depth + 1)
        if isinstance(node.op, ast.Pow) and abs(right) > 1000:
            raise ValueError("Exponent is too large")
        value = _BINARY[type(node.op)](left, right)
    elif isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY:
        value = _UNARY[type(node.op)](_evaluate(node.operand, depth=depth + 1))
    else:
        raise ValueError("Only numeric literals and arithmetic operators are allowed")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("Result is not finite")
    if isinstance(value, int) and value.bit_length() > 16384:
        raise ValueError("Integer result is too large")
    return value


def calculator_tool() -> Tool:
    async def execute(
        arguments: Mapping[str, Any], _context: ToolExecutionContext
    ) -> dict[str, Any]:
        expression = str(arguments["expression"])
        if len(expression) > 500:
            raise ValueError("Expression is too long")
        try:
            tree = ast.parse(expression, mode="eval")
        except SyntaxError as exc:
            raise ValueError("Invalid arithmetic expression") from exc
        if sum(1 for _ in ast.walk(tree)) > 100:
            raise ValueError("Expression is too complex")
        return {"expression": expression, "value": _evaluate(tree)}

    return Tool(
        name="calculator",
        description="Evaluate a basic arithmetic expression safely.",
        parameters={
            "type": "object",
            "properties": {
                "expression": {
                    "type": "string",
                    "description": "Arithmetic expression, for example (17 + 5) * 3.",
                }
            },
            "required": ["expression"],
            "additionalProperties": False,
        },
        executor=execute,
    )

