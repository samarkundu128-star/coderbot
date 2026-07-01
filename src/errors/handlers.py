```python
import structlog

logger = structlog.get_logger(__name__)

class BaseAppException(Exception):
    """Root architectural exception."""
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message

class DatabaseTransactionError(BaseAppException):
    """Database operations failure handle karne ke liye."""
    pass

class AIServiceException(BaseAppException):
    """Gemini AI API errors ke liye."""
    pass

class TelegramDeliveryException(BaseAppException):
    """Telegram message delivery errors ke liye."""
    pass

```
