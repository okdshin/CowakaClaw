import openai


class CowakaClawAgent:
    def __init__(self, model):
        self.model = model
        self.openai_client = openai.OpenAI()

    def agent_loop(self):
        messages = []
        while True:
            user_message = input("> ")
            messages.append({"role": "user", "content": user_message})
            response = self.openai_client.chat.completions.create(
                messages=messages, model=self.model
            )
            messages.append(
                {"role": "assistant", "content": response.choices[0].message.content}
            )
            print(response.choices[0].message.content)


def main():
    agent = CowakaClawAgent(model="openai-gpt-oss-20b")
    agent.agent_loop()


if __name__ == "__main__":
    main()
