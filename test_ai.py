import os
from openai import OpenAI

client = OpenAI(
    base_url="https://api.groq.com/openai/v1",
    api_key=os.getenv("GROQ_API_KEY"),
)

def test_groq_connection():
    print("Sending test request to Groq...")
    try:
        response = client.chat.completions.create(
            model="openai/gpt-oss-20b",
            messages=[
                {"role": "system", "content": "You are a helpful assistant. Reply strictly in JSON format."},
                {"role": "user", "content": "Say hello and confirm you are ready to review code."}
            ],
            response_format={"type": "json_object"}
        )
        print("\n--- AI Response Received Successfully! ---")
        print(response.choices[0].message.content)
        print("------------------------------------------")
    except Exception as e:
        print(f"\n[ERROR] Connection failed: {e}")

if __name__ == "__main__":
    test_groq_connection()
