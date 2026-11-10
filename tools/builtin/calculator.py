"""计算数学表达式，支持 + - * / // % ** 与括号。"""
import ast
import math
import operator
from errors import ToolError

SCHEMA = {
    "name": "calculator",
    "description": "计算数学表达式，支持 + - * / // % ** 与括号",
    "parameters": {
        "type": "object",
        "properties": {
            "expression": {
                "type": "string",
                "maxLength": 200
            }
        },
        "required": [
            "expression"
        ],
        "additionalProperties": False
    }
}


def execute(context, expression):
    if len(expression) > 200:
        raise ToolError('表达式过长')
    ops = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
           ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod,
           ast.Pow: operator.pow, ast.USub: operator.neg, ast.UAdd: operator.pos}
    def evaluate(node):
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            value = node.value
        elif isinstance(node, ast.UnaryOp) and type(node.op) in (ast.USub, ast.UAdd):
            value = ops[type(node.op)](evaluate(node.operand))
        elif isinstance(node, ast.BinOp) and type(node.op) in ops:
            left, right = evaluate(node.left), evaluate(node.right)
            if isinstance(node.op, ast.Pow) and abs(right) > 100:
                raise ToolError('指数过大')
            value = ops[type(node.op)](left, right)
        else:
            raise ToolError('不支持的表达式')
        if not isinstance(value, (float, int)) or abs(value) > 1e100 or not math.isfinite(value):
            raise ToolError('结果超出范围')
        return value
    try:
        return evaluate(ast.parse(expression, mode='eval').body)
    except (SyntaxError, ArithmeticError, RecursionError) as exc:
        raise ToolError('表达式语法错误') from exc
