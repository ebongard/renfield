"""
Standardized plugin response formatting
"""
from typing import Dict, Any, Optional


class PluginResponse:
    """Utility class for creating standardized plugin responses"""

    @staticmethod
    def success(
        message: str,
        data: Optional[Dict[str, Any]] = None,
        action_taken: bool = True
    ) -> Dict[str, Any]:
        """Create success response"""
        return {
            "success": True,
            "message": message,
            "action_taken": action_taken,
            "data": data or {}
        }

    @staticmethod
    def error(
        message: str,
        error_code: Optional[str] = None,
        action_taken: bool = False
    ) -> Dict[str, Any]:
        """Create error response"""
        response = {
            "success": False,
            "message": message,
            "action_taken": action_taken
        }

        if error_code:
            response["error_code"] = error_code

        return response

    @staticmethod
    def not_found(entity: str) -> Dict[str, Any]:
        """Create not found response"""
        return PluginResponse.error(
            message=f"{entity} not found",
            error_code="NOT_FOUND"
        )

    @staticmethod
    def invalid_parameters(details: str) -> Dict[str, Any]:
        """Create invalid parameters response"""
        return PluginResponse.error(
            message=f"Invalid parameters: {details}",
            error_code="INVALID_PARAMETERS"
        )
