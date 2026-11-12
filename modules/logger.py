import logging
import logging.handlers
import sys

def setup_logger(log_level: str = "INFO", syslog_server: str = "", log_file: str = ""):
    """
    Configures the root logger for the entire application.
    All modules should use `logging.getLogger(__name__)` after this is called.
    """
    # Convert string level to logging integer (e.g., "DEBUG" -> 10)
    numeric_level = getattr(logging, log_level.upper(), logging.INFO)
    
    # Configure Root Logger
    root_logger = logging.getLogger()
    root_logger.setLevel(numeric_level)
    
    # Remove default handlers if they exist (prevents duplicate logs on reload)
    if root_logger.hasHandlers():
        root_logger.handlers.clear()
        
    # Standard formatter for console and files
    formatter = logging.Formatter('%(asctime)s - %(name)s - [%(levelname)s] - %(message)s')
    
    # 1. Console Stream Handler (replaces standard print)
    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)
    
    # 2. Local File Handler (Optional)
    if log_file:
        try:
            file_handler = logging.FileHandler(log_file)
            file_handler.setFormatter(formatter)
            root_logger.addHandler(file_handler)
        except Exception as e:
            root_logger.error(f"Failed to configure FileHandler for '{log_file}': {e}")

    # 3. Remote Syslog Handler (Optional)
    if syslog_server:
        try:
            ip, port = syslog_server.split(':')
            syslog_handler = logging.handlers.SysLogHandler(address=(ip, int(port)))
            
            # Syslog usually has its own timestamp, so we use a simpler formatter
            syslog_formatter = logging.Formatter('NB Client Engine: %(name)s - [%(levelname)s] - %(message)s')
            syslog_handler.setFormatter(syslog_formatter)
            
            root_logger.addHandler(syslog_handler)
            root_logger.info(f"Syslog forwarding enabled to {syslog_server}")
        except ValueError:
            root_logger.error(f"Invalid syslog server format '{syslog_server}'. Expected IP:PORT.")
        except Exception as e:
            root_logger.error(f"Failed to configure SysLogHandler: {e}")