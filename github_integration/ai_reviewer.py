import os
from openai import OpenAI

# Initialize the client pointing to Groq's  endpoint
client = OpenAI(
    base_url="https://api.groq.com/openai/v1",
    api_key=os.environ.get("GROQ_API_KEY"),
)

def generate_ai_code_review(diff_content):
    response = client.chat.completions.create(
        model="llama-3.3-70b-versatile", # Free, powerful open-source model
        messages=[
            {
                "role": "system",
                "content": "You are an expert code reviewer. Return valid JSON with keys: 'summary', 'issues', and 'score'."
            },
            {
                "role": "user", 
                "content": f"Review this code diff:\n{diff_content}"
            }
        ],
        response_format={"type": "json_object"}
    )
    return response.choices[0].message.content