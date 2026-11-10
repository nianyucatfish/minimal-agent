"""替换当前会话的任务便笺：目标、约束、已验证发现与下一步；最多800字符。空字符串清除。。"""

from errors import ToolError

SCHEMA = {
    "name": "update_working_checkpoint",
    "description": "替换当前会话的任务便笺：目标、约束、已验证发现与下一步；最多800字符。空字符串清除。",
    "parameters": {
        "type": "object",
        "properties": {
            "key_info": {
                "type": "string",
                "maxLength": 800
            }
        },
        "required": [
            "key_info"
        ],
        "additionalProperties": False
    }
}


def execute(context, key_info):
    if len(key_info) > 800:
        raise ToolError('checkpoint 最多800字符，请精简后重试')
    context.state['checkpoint'] = key_info.strip()
    return {'status': 'updated'}
