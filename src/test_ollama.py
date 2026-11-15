
import ollama

response = ollama.chat(
    model="qwen2.5:3b",
    messages=[
        {
            "role": "user",
            "content": "Explique o que é SQL em poucas palavras."
        }
    ]
)

print(response["message"]["content"])
