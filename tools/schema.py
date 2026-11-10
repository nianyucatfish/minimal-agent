"""A deliberately small JSON Schema subset; unsupported keywords fail at registration."""
import math

TYPES = {'string': lambda v: isinstance(v, str), 'boolean': lambda v: type(v) is bool,
         'integer': lambda v: type(v) is int,
         'number': lambda v: type(v) is int or (type(v) is float and math.isfinite(v)),
         'object': lambda v: isinstance(v, dict), 'array': lambda v: isinstance(v, list),
         'null': lambda v: v is None}
KEYWORDS = {'type', 'description', 'enum', 'properties', 'required', 'additionalProperties',
            'items', 'minLength', 'maxLength', 'minimum', 'maximum', 'minItems', 'maxItems'}


def check_schema(schema):
    if not isinstance(schema, dict) or set(schema) - KEYWORDS or schema.get('type') not in TYPES:
        raise ValueError('Schema 缺少有效 type 或使用了不支持的关键字')
    kind = schema['type']
    if 'enum' in schema and (not isinstance(schema['enum'], list) or not schema['enum']):
        raise ValueError('enum 必须是非空列表')
    for lower, upper, allowed in [('minLength', 'maxLength', {'string'}),
                                  ('minItems', 'maxItems', {'array'}),
                                  ('minimum', 'maximum', {'number', 'integer'})]:
        for key in (lower, upper):
            if key in schema:
                value = schema[key]
                if kind not in allowed or type(value) not in (int, float) or not math.isfinite(value):
                    raise ValueError('Schema 边界类型错误')
                if key not in ('minimum', 'maximum') and (type(value) is not int or value < 0):
                    raise ValueError('长度边界必须为非负整数')
        if lower in schema and upper in schema and schema[lower] > schema[upper]:
            raise ValueError('Schema 最小值不能超过最大值')
    if kind == 'object':
        properties, required = schema.get('properties', {}), schema.get('required', [])
        if (not isinstance(properties, dict) or not isinstance(required, list) or
            any(not isinstance(k, str) for k in properties) or
            any(not isinstance(k, str) or k not in properties for k in required) or
            type(schema.get('additionalProperties', False)) is not bool):
            raise ValueError('对象 Schema 字段不合法')
        for child in properties.values():
            check_schema(child)
    elif any(k in schema for k in ('properties', 'required', 'additionalProperties')):
        raise ValueError('对象关键字只能用于 object')
    if kind == 'array':
        check_schema(schema.get('items'))
    elif 'items' in schema:
        raise ValueError('items 只能用于 array')


def validate(value, schema, path='arguments'):
    if not TYPES[schema['type']](value):
        raise ValueError(f'{path} 类型错误，需要 {schema["type"]}')
    if 'enum' in schema and value not in schema['enum']:
        raise ValueError(f'{path} 不在允许的取值中')
    if isinstance(value, dict):
        properties = schema.get('properties', {})
        if set(schema.get('required', [])) - set(value):
            raise ValueError(f'{path} 缺少必填字段')
        if not schema.get('additionalProperties', False) and set(value) - set(properties):
            raise ValueError(f'{path} 包含未知字段')
        for key, item in value.items():
            if key in properties:
                validate(item, properties[key], f'{path}.{key}')
    elif isinstance(value, list):
        for index, item in enumerate(value):
            validate(item, schema['items'], f'{path}[{index}]')
    for key, actual, compare in [('minLength', len(value) if isinstance(value, str) else 0, lambda a,b:a<b),
                                 ('maxLength', len(value) if isinstance(value, str) else 0, lambda a,b:a>b),
                                 ('minItems', len(value) if isinstance(value, list) else 0, lambda a,b:a<b),
                                 ('maxItems', len(value) if isinstance(value, list) else 0, lambda a,b:a>b),
                                 ('minimum', value, lambda a,b:a<b), ('maximum', value, lambda a,b:a>b)]:
        if key in schema and compare(actual, schema[key]):
            raise ValueError(f'{path} 不满足 {key}={schema[key]}')
