import os
from openai import OpenAI

CONFIG = {
    "openai": ("OPENAI_API_KEY", None, "gpt-4.1-mini"),
    "deepseek": ("DEEPSEEK_API_KEY", "https://api.deepseek.com", "deepseek-chat"),
    "qwen": ("QWEN_API_KEY", "https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-plus"),
    "kimi": ("KIMI_API_KEY", "https://api.moonshot.cn/v1", "moonshot-v1-8k"),
    # 豆包需按火山引擎控制台实际 endpoint-id 修改模型名
    "doubao": ("DOUBAO_API_KEY", "https://ark.cn-beijing.volces.com/api/v3", "YOUR_DOUBAO_ENDPOINT_ID"),
}

def ask(model_name: str, system: str, user: str) -> str:
    key_name, base_url, model = CONFIG[model_name]
    key = os.getenv(key_name)
    if not key:
        return f"未配置 {key_name}，该角色未调用。"
    client = OpenAI(api_key=key, base_url=base_url)
    response = client.chat.completions.create(
        model=model,
        temperature=0.2,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
    )
    return response.choices[0].message.content or "模型未返回内容"
