from openai import OpenAI

client = OpenAI(
  base_url="http://127.0.0.1:8000/v1",
  api_key="test",
)

completion = client.chat.completions.create(
  extra_headers={
    "HTTP-Referer": "",
    "X-Title": "",
  },
  stream=True,
  model="Qwen/Qwen2.5-72B-Instruct",

  messages=[
    {
      "role": "user",
      "content": "现在几点了 ? 有哪些容器？ 北京天气  ？ "
    }
  ]
)

for chunk in completion:
    if chunk.choices[0].delta.content:
        print(chunk.choices[0].delta.content, end="", flush=True)


