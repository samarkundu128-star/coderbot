import json
import structlog
import google.generativeai as genai
from typing import List, Dict, Any
from src.config.settings import settings
from src.errors.handlers import AIServiceException

logger = structlog.get_logger(__name__)

class AICodingEngine:
    def __init__(self):
        try:
            # Gemini client ko configure kiya ja raha hai
            genai.configure(api_key=settings.GEMINI_API_KEY.get_secret_value())
            # Hum production-ready speed aur accuracy ke liye gemini-2.5-flash-preview-09-2025 use karenge
            self.model = genai.GenerativeModel("gemini-2.5-flash-preview-09-2025")
        except Exception as e:
            logger.critical("Google Gemini SDK configuration failed!", error=str(e))
            raise AIServiceException(f"AI Service configuration error: {str(e)}")

    def _build_system_instruction(self) -> str:
        # AI ko strict coding guidelines dene ke liye system prompt
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
            contents = []
            # Purani chat history ko add kiya ja raha hai context ke liye
            for h in history:
                contents.append({"role": "user" if h["role"] == "user" else "model", "parts": [h["content"]]})
            
            # System instructions ke saath naya user prompt inject kiya ja raha hai
            contents.append({"role": "user", "parts": [f"{self._build_system_instruction()}\nUser Request: {prompt}"]})
            
            response = await self.model.generate_content_async(
                contents=contents,
                generation_config={
                    "temperature": 0.2, # Kam temperature taaki bugs na aayein
                    "top_p": 0.95,
                    "max_output_tokens": 8192
                }
            )
            
            if not response.text:
                raise AIServiceException("AI service returned empty response!")
                
            return self._parse_response(response.text)
        except Exception as e:
            logger.error("Response generation failed!", error=str(e))
            raise AIServiceException(f"Model generation error: {str(e)}")

    def _parse_response(self, text: str) -> Dict[str, Any]:
        # JSON output aur text commentary ko alag-alag karne ka logic
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
      
