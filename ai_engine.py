import json
import structlog
import os
from groq import Groq
from typing import List, Dict, Any
from src.config.settings import settings
from src.errors.handlers import AIServiceException

logger = structlog.get_logger(__name__)

class AICodingEngine:
    def __init__(self):
        try:
            # Groq client ko initialize kiya ja raha hai safely
            # Agar settings me GROQ_API_KEY hai toh use karenge, nahi toh env se uthayenge
            try:
                api_key = settings.GROQ_API_KEY.get_secret_value()
            except Exception:
                api_key = os.getenv("GROQ_API_KEY")

            if not api_key:
                raise ValueError("GROQ_API_KEY missing hai settings aur environment dono me!")

            self.client = Groq(api_key=api_key)
            # Production coding ke liye Llama 3 70B standard aur powerful model hai
            self.model = "llama3-70b-8192"
            print("✅ Groq AI Coding Engine successfully initialize ho gaya!")
        except Exception as e:
            logger.critical("Groq SDK configuration failed!", error=str(e))
            raise AIServiceException(f"AI Service configuration error: {str(e)}")

    def _build_system_instruction(self) -> str:
        return (
            "Aap ek Elite AI Coding Assistant hain. Har request ka safe aur complete runnable code generate karein.\n"
            "Saare generated files ko strictly is JSON array structure me hi return karein:\n"
            "[\n"
            "  {\n"
            "    \"file_path\": \"folder_name/filename.ext\",\n"
            "    \"content\": \"Yahan aapka pura code aayega\"\n"
            "  }\n"
            "]\n"
            "Baaki saara explanation aur notes is JSON block ke bahar simple language me likhein."
        )

    async def generate_solution(self, prompt: str, history: List[Dict[str, str]]) -> Dict[str, Any]:
        try:
            # Messages array ready kar rahe hain Groq chat completion ke liye
            messages = [{"role": "system", "content": self._build_system_instruction()}]
            
            # History maps ko Groq format me convert kar rahe hain
            for h in history:
                role = "assistant" if h["role"] == "model" else h["role"]
                messages.append({"role": role, "content": h["content"]})
                
            # Latest prompt add kar rahe hain
            messages.append({"role": "user", "content": prompt})

            # Groq API Sync call ko wrapped rakhenge async workflow ke hisab se
            chat_completion = self.client.chat.completions.create(
                messages=messages,
                model=self.model,
                temperature=0.2, # Kam temperature taaki bugs na aayein
                max_tokens=4096,
                top_p=0.95
            )

            response_text = chat_completion.choices[0].message.content

            if not response_text:
                raise AIServiceException("Groq AI service returned empty response!")

            return self._parse_response(response_text)
        except Exception as e:
            logger.error("Response generation failed!", error=str(e))
            raise AIServiceException(f"Model generation error: {str(e)}")

    def _parse_response(self, text: str) -> Dict[str, Any]:
        try:
            start_idx = text.find("[")
            end_idx = text.rfind("]") + 1
            if start_idx != -1 and end_idx != -1:
                json_str = text[start_idx:end_idx]
                files = json.loads(json_str)
                commentary = text[:start_idx] + text[end_idx:]
                return {"files": files, "commentary": commentary.strip()}
        except Exception:
            pass
        return {"files": [], "commentary": text}
