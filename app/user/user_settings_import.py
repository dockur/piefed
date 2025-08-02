"""
User Settings Import Module

This module handles the import functionality for user settings, including
file validation, JSON parsing, and discriminated union types for type-safe
result handling.
"""

import os
import json as python_json
from typing import Optional, Union
from dataclasses import dataclass

from flask import abort
from app.utils import gibberish


DEFAULT_MAX_FILE_SIZE = 1 * 1024 * 1024  # 1MB in bytes


@dataclass
class NoFileSubmitted:
    error_message: str

@dataclass
class InvalidFileType:
    error_message: str

@dataclass
class FileTooLarge:
    error_message: str

@dataclass
class InvalidJson:
    error_message: str

@dataclass
class ValidImport:
    final_place: str

SettingsImportError = Union[NoFileSubmitted, InvalidFileType, FileTooLarge, InvalidJson]
SettingsImportResult = Union[SettingsImportError, ValidImport]


def remap_filename(import_file) -> Optional[str]:
    """
    Process an uploaded import file and return the file path where it should be saved.
    
    This is a pure function that handles file validation and filename generation
    without side effects. The caller is responsible for actually saving the file
    and showing appropriate error messages for None returns.
    
    Args:
        import_file: The uploaded file object from Flask request
        
    Returns:
        str: The full path where the file should be saved
        None: If no file uploaded or filename is empty (caller should show error)
        
    Raises:
        400 HTTP error: If file has invalid extension (not .json)
    """
    if not import_file or not import_file.filename:
        return None
        
    if not import_file.filename.lower().endswith('.json'):
        abort(400)
        
    # Generate unique filename with single .json extension
    new_filename = gibberish(15) + '.json'
    directory = 'app/static/media/'
    final_place = os.path.join(directory, new_filename)
    
    return final_place


def validate_json(import_file, max_file_size: int = DEFAULT_MAX_FILE_SIZE) -> Optional[SettingsImportError]:
    """
    Validate that an uploaded file contains valid JSON content with size limits.
    
    This function reads the file content and validates it as JSON without
    side effects. The file stream position is reset after validation.
    Includes protection against oversized files.
    
    Args:
        import_file: The uploaded file object from Flask request
        max_file_size: Maximum allowed file size in bytes (default: 10MB)
        
    Returns:
        None: If the file is valid JSON
        SettingsImportError: Specific error type (FileTooLarge, InvalidJson, etc.)
    """
    if not import_file:
        return InvalidJson("No file provided for validation.")
    
    MAX_FILE_SIZE = max_file_size
    
    try:
        import_file.stream.seek(0)
        
        # Check file size before reading entire content
        import_file.stream.seek(0, 2)
        file_size = import_file.stream.tell()
        import_file.stream.seek(0)
        
        if file_size > MAX_FILE_SIZE:
            max_mb = MAX_FILE_SIZE / (1024*1024)
            return FileTooLarge(f"File too large: {file_size / (1024*1024):.1f}MB (max {max_mb:.0f}MB)")
        
        if file_size == 0:
            return InvalidJson("Empty file uploaded. Please select a file with content.")
        
        content = import_file.stream.read(MAX_FILE_SIZE).decode('utf-8')
        
        # Validate JSON
        python_json.loads(content)
        
        # Reset for saving
        import_file.stream.seek(0)
        return None 
        
    except MemoryError:
        try:
            import_file.stream.seek(0)
        except:
            pass
        return FileTooLarge("File too large to process in memory. Please use a smaller file.")
        
    except UnicodeDecodeError as e:
        try:
            import_file.stream.seek(0)
        except:
            pass
        return InvalidJson(f"File encoding error: {str(e)}")
        
    except python_json.JSONDecodeError as e:
        try:
            import_file.stream.seek(0)
        except:
            pass
        return InvalidJson(f"Invalid JSON format: {str(e)}")
        
    except Exception as e:
        try:
            import_file.stream.seek(0)
        except:
            pass
        return InvalidJson(f"Unexpected error validating file: {str(e)}")


def process_settings_import(request) -> SettingsImportResult:
    """
    Process a settings import request and return a discriminated union result.
    
    This function encapsulates all the validation logic for settings import
    and returns a type-safe result that can be pattern matched.
    
    Args:
        request: The Flask request object containing the uploaded file
        
    Returns:
        SettingsImportResult: One of NoFileSubmitted, InvalidFileType, 
                             FileTooLarge, InvalidJson, or ValidImport
    """
    
    if 'import_file' not in request.files:
        return NoFileSubmitted("No file was uploaded. Please select a valid JSON file to import.")
    
    import_file = request.files['import_file']
    
    final_place = remap_filename(import_file)
    if final_place is None:
        return InvalidFileType("Please select a valid JSON file to import.")
    
    json_validation_error = validate_json(import_file)
    if json_validation_error is not None:
        return json_validation_error
    
    return ValidImport(final_place)