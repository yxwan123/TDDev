from openai import OpenAI
import time
from tqdm import tqdm

class Bot:
    def __init__(self, key, patience=1) -> None:
        self.key = key
        self.patience = patience
    
    def ask(self):
        raise NotImplementedError


class OpenAILLM(Bot):
    def __init__(self, key, base_url=None, patience=1, model="gpt-4.1") -> None:
        super().__init__(key, patience)
        if base_url:
            self.client = OpenAI(
                api_key=self.key,
                base_url=base_url
            )
        else:
            self.client = OpenAI(api_key=self.key)
        self.model = model
        
    def ask(self, question, image_encoding=None, image_encoding2=None, verbose=False):
        print(f"Querying {self.model}, please wait...")
        
        if image_encoding:
            if image_encoding2:
                print("2 images")
                content = {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": question},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/png;base64,{image_encoding}",
                            },
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/png;base64,{image_encoding2}",
                            },
                        },
                    ],
                }
            else:
                print("1 image")
                content = {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": question},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/png;base64,{image_encoding}",
                            },
                        },
                    ],
                }


        else:
            print("text only")
            content = {"role": "user", "content": question}
        while True:
            try:
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=[
                        content
                    ],
                )
                response = response.choices[0].message.content
                break
            except Exception as e:
                print("\n\n\n")
                print(f"⚠️⚠️⚠️openai error: {e}")
                print(f"⛔️⛔️⛔️type: {type(e).__name__}")
                print(f"🚨🚨🚨details: {str(e)}")
                print("⏰⏰⏰ Pending...Try to fix it! 🧰🧰🧰")
                for _ in tqdm(range(60), desc="Retry after:"):
                    time.sleep(1)
        if verbose:
            print("####################################")
            print("question:\n", question)
            print("####################################")
            print("response:\n", response)
            print("\n\n\n\n\n\n\n")

        response_clean = response.strip()
        if response_clean.startswith('```json'):
            response_clean = response_clean[len('```json'):].strip()
        if response_clean.endswith('```'):
            response_clean = response_clean[:-3].strip()

        return response_clean