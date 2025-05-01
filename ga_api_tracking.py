import json
import os
import threading
import uuid
import time
from datetime import datetime
from functools import wraps
from typing import Dict, Any, Optional, Callable

import requests

# Try to import machineid for better client identification
try:
    import machineid
    HAVE_MACHINEID = True
except ImportError:
    HAVE_MACHINEID = False


# Debug mode controlled by environment variable
_DEBUG = os.getenv("GA_DEBUG_ANALYTICS")


def disable_request_logs():
    """Context manager to temporarily disable request logging"""
    import logging
    
    class LoggingFilter(logging.Filter):
        def filter(self, record):
            return False
    
    root_logger = logging.getLogger()
    request_logger = logging.getLogger("requests")
    urllib3_logger = logging.getLogger("urllib3")
    previous_level = root_logger.level
    previous_filters = {
        "root": list(root_logger.filters),
        "requests": list(request_logger.filters),
        "urllib3": list(urllib3_logger.filters),
    }
    log_filter = LoggingFilter()
    
    class DisableRequestLogs:
        def __enter__(self):
            if previous_level > logging.INFO:
                root_logger.setLevel(logging.INFO)
            root_logger.addFilter(log_filter)
            request_logger.addFilter(log_filter)
            urllib3_logger.addFilter(log_filter)
            return self
        
        def __exit__(self, exc_type, exc_val, exc_tb):
            root_logger.setLevel(previous_level)
            root_logger.filters = previous_filters["root"]
            request_logger.filters = previous_filters["requests"]
            urllib3_logger.filters = previous_filters["urllib3"]
    
    return DisableRequestLogs()


def is_gdpr_country() -> bool:
    """
    Check if the current IP is from a GDPR country.
    Returns True if in GDPR country or if check fails.
    """
    try:
        response = requests.get("https://ipinfo.io/json", timeout=2)
        if response.status_code == 200:
            data = response.json()
            # List of GDPR countries (EU member states + UK, Norway, Iceland, Liechtenstein, Switzerland)
            gdpr_countries = [
                "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", 
                "DE", "GR", "HU", "IE", "IT", "LV", "LT", "LU", "MT", "NL", 
                "PL", "PT", "RO", "SK", "SI", "ES", "SE", "GB", "UK", "NO", 
                "IS", "LI", "CH"
            ]
            return data.get("country", "") in gdpr_countries
        return True  # If can't determine, assume GDPR to be safe
    except Exception:
        return True  # If check fails, assume GDPR to be safe


def analytics_disabled() -> bool:
    """
    Check if analytics should be disabled based on environment variables or GDPR status
    """
    env_disabled = os.getenv("GA_DISABLE_ANALYTICS")
    return bool(env_disabled) or is_gdpr_country()


class GoogleAnalyticsTracker:
    """Google Analytics event tracker for general usage"""
    
    @staticmethod
    def get_client_id():
        """Get a stable client ID based on machine ID if available, otherwise UUID"""
        if HAVE_MACHINEID:
            try:
                return str(uuid.UUID(machineid.id()))
            except Exception:
                pass
        return str(uuid.uuid4())
    
    def __init__(
        self, 
        measurement_id: str, 
        api_secret: str, 
        package_name: str,
        package_version: str,
        client_id: Optional[str] = None,
        package_params: Optional[Dict[str, str]] = None
    ):
        """
        Initialize Google Analytics tracker
        
        Args:
            measurement_id: GA4 measurement ID (format: G-XXXXXXXX)
            api_secret: GA4 API secret
            package_name: Name of the package using this tracker
            package_version: Version string of the package
            client_id: Optional client ID (will use machine ID or generate UUID if not provided)
            package_params: Optional default parameters to send with each event
        """
        self._disabled = analytics_disabled()
        
        # Configure API endpoint with debug mode if enabled
        base_url = (
            "https://www.google-analytics.com/mp/collect"
            if not _DEBUG else 
            "https://www.google-analytics.com/debug/mp/collect"
        )
        self.base_url = f"{base_url}?measurement_id={measurement_id}&api_secret={api_secret}"
        
        self._client_id = client_id or self.get_client_id() if not self._disabled else None
        self._package = package_name
        self._version = package_version
        self._package_params = package_params or {}
    
    def send_event_decorator(
        self,
        event_name: str,
        event_params: Optional[Dict[str, str]] = None
    ) -> Callable:
        """
        Decorator to send an event when the decorated function is called
        
        Args:
            event_name: Name of the event to send
            event_params: Optional parameters to include with the event
            
        Returns:
            Decorator function
        """
        def decorator(func):
            @wraps(func)
            def wrapper(*args, **kwargs):
                # Send event before executing the function
                self.send_event(event_name, event_params)
                # Call the original function
                return func(*args, **kwargs)
            return wrapper
        return decorator
    
    def send_event(
        self,
        event_name: str,
        event_params: Optional[Dict[str, Any]] = None,
        user_id: Optional[str] = None,
        raise_errors: bool = False,
        _await_response: bool = False,
    ) -> None:
        """
        Send an event to Google Analytics
        
        Args:
            event_name: Name of the event
            event_params: Optional parameters for the event
            user_id: Optional user ID for the event
            raise_errors: True to raise any errors that occur
            _await_response: For testing - wait for request thread to complete
        """
        if self._disabled:
            if _await_response:
                raise ValueError("Cannot await response when analytics is disabled")
            return
        
        if not event_params:
            event_params = {}
            
        def _send_request():
            params_copy = event_params.copy()  # Create a copy to avoid modifying the original
            
            # Add package info to parameters
            params_copy.update(self._package_params)
            params_copy["package"] = self._package
            params_copy["version"] = self._version
            
            # Create the payload
            payload = {
                "client_id": self._client_id,
                "events": [{"name": event_name, "params": params_copy}],
            }
            
            # Add user ID if provided
            if user_id:
                payload["user_id"] = user_id
            
            headers = {"Content-Type": "application/json"}
            data = json.dumps(payload)
            
            # Send request without logging
            with disable_request_logs():
                try:
                    response = requests.post(self.base_url, headers=headers, data=data)
                    response.raise_for_status()
                    
                    if _DEBUG:
                        print(f"GA Debug: {response.content}")
                except Exception as exception:
                    if _DEBUG:
                        print(f"GA Error: {str(exception)}")
                    
                    if raise_errors:
                        raise exception
        
        # Run in background thread
        thread = threading.Thread(target=_send_request)
        thread.start()
        
        # For testing only - wait for thread to complete
        if _await_response:
            thread.join()


# Create reusable tracker based on environment variables (for easy integration)
def create_default_tracker(package_name: str, version: str) -> GoogleAnalyticsTracker:
    """Create a tracker using environment variables for configuration"""
    measurement_id = os.getenv("GA_MEASUREMENT_ID")
    api_secret = os.getenv("GA_API_SECRET")
    
    if not measurement_id or not api_secret:
        # Return disabled tracker if environment variables are not set
        # Still create an instance so calling code doesn't need to check for None
        return GoogleAnalyticsTracker(
            measurement_id="G-XXXXXXXX", 
            api_secret="secret",
            package_name=package_name,
            package_version=version
        )
    
    return GoogleAnalyticsTracker(
        measurement_id=measurement_id,
        api_secret=api_secret,
        package_name=package_name,
        package_version=version
    )


# Example with basic events
def example_usage():
    # Create tracker
    tracker = GoogleAnalyticsTracker(
        measurement_id=os.getenv("GA_MEASUREMENT_ID", "G-XXXXXXXXXX"),
        api_secret=os.getenv("GA_API_SECRET", "YOUR_API_SECRET"),
        package_name="example_app",
        package_version="1.0.0"
    )
    
    # Example of using a decorator
    @tracker.send_event_decorator("function_executed", {"category": "demo"})
    def example_function(param):
        print(f"Processing {param}")
        return f"Result: {param}"
    
    # Manually sending an event
    def manual_event_example():
        # Send a simple event
        tracker.send_event(
            event_name="button_clicked",
            event_params={
                "button_id": "submit-form",
                "page": "checkout"
            }
        )
        
        # Send an event with user identification
        tracker.send_event(
            event_name="login_successful",
            event_params={"method": "email"},
            user_id="user-123"
        )
    
    return example_function, manual_event_example


if __name__ == "__main__":
    # Create tracker with environment variables
    tracker = GoogleAnalyticsTracker(
        measurement_id=os.getenv("GA_MEASUREMENT_ID", "G-XXXXXXXXXX"),
        api_secret=os.getenv("GA_API_SECRET", "YOUR_API_SECRET"),
        package_name="example_app",
        package_version="1.0.0"
    )
    
    # Example function with decorator
    @tracker.send_event_decorator("example_function_called", {"demo": "true"})
    def example_function():
        print("Function called and tracked")
        return "Function result"
    
    # Call the decorated function
    result = example_function()
    print(f"Function returned: {result}")
    
    # Send a manual event
    tracker.send_event(
        event_name="manual_event",
        event_params={
            "category": "test",
            "action": "click",
            "label": "button",
            "value": 1
        },
        user_id="user123"
    )
    
    print("Event tracked in the background")
    
    # Give time for background thread to complete
    time.sleep(1) 