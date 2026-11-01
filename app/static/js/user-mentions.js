// User mention autocompletion functionality

// Global state for user mention suggestions
let activeSuggestionBox = null;
let activeSuggestionTextarea = null;
let currentSuggestions = [];
let currentSuggestionIndex = -1;

function setupUserMentionSuggestions() {
    // Set up event listeners for all textareas on page load
    document.querySelectorAll('textarea').forEach(textarea => {
        addUserMentionListener(textarea);
    });
}

function addUserMentionListener(textarea) {
    if (textarea.dataset.mentionListenerSetup) return;
    
    textarea.addEventListener('input', handleTextareaInput);
    textarea.addEventListener('keydown', handleTextareaKeydown);
    textarea.addEventListener('blur', hideUserSuggestions);
    textarea.dataset.mentionListenerSetup = 'true';
}

function handleTextareaInput(event) {
    const textarea = event.target;
    const cursorPos = textarea.selectionStart;
    const textBeforeCursor = textarea.value.substring(0, cursorPos);
    
    // Look for @ or ! or ~ symbol preceded by whitespace, start of string, or newline
    // This avoids triggering in email addresses or other contexts
    const mentionMatch = textBeforeCursor.match(/(?:^|\s|[\r\n])[@!~]([a-zA-Z0-9_.-]*?)$/);
    
    if (mentionMatch && mentionMatch[1] !== undefined) {
        const query = mentionMatch[1];
        const fullMatch = mentionMatch[0];
        const trigger = fullMatch.charAt(fullMatch.length - 1 - query.length); // @ or ! or ~
        
        if (query.length >= 2) {
            // Only show suggestions after typing 2+ letters
            showUserSuggestions(textarea, trigger + query);
        } else {
            hideUserSuggestions();
        }
    } else {
        hideUserSuggestions();
    }
}

function handleTextareaKeydown(event) {
    if (!activeSuggestionBox || activeSuggestionTextarea !== event.target) return;
    
    const suggestions = activeSuggestionBox.querySelectorAll('.user-suggestion-item');
    
    if (event.key === 'ArrowDown') {
        event.preventDefault();
        currentSuggestionIndex = Math.min(currentSuggestionIndex + 1, suggestions.length - 1);
        updateSuggestionSelection();
    } else if (event.key === 'ArrowUp') {
        event.preventDefault();
        currentSuggestionIndex = Math.max(currentSuggestionIndex - 1, 0);
        updateSuggestionSelection();
    } else if (event.key === 'Enter' || event.key === 'Tab') {
        event.preventDefault();
        if (currentSuggestionIndex >= 0 && suggestions[currentSuggestionIndex]) {
            selectSuggestion(suggestions[currentSuggestionIndex].textContent);
        }
    } else if (event.key === 'Escape') {
        hideUserSuggestions();
    }
}

async function showUserSuggestions(textarea, query) {
    if (query.length < 3) return; // Minimum: trigger char + 2 letters
    
    try {
        const response = await fetch(`/api/alpha/suggest_completion?q=${encodeURIComponent(query)}`);
        const data = await response.json();
        
        if (data.result && data.result.length > 0) {
            displaySuggestions(textarea, data.result);
        } else {
            hideUserSuggestions();
        }
    } catch (error) {
        console.error('Error fetching user suggestions:', error);
        hideUserSuggestions();
    }
}

function displaySuggestions(textarea, suggestions) {
    hideUserSuggestions(); // Clean up any existing suggestion box
    
    activeSuggestionTextarea = textarea;
    currentSuggestions = suggestions;
    currentSuggestionIndex = 0;
    
    // Create suggestion box
    const suggestionBox = document.createElement('div');
    suggestionBox.className = 'user-suggestion-box';
    
    suggestions.forEach((suggestion, index) => {
        const item = document.createElement('div');
        item.className = 'user-suggestion-item';
        item.textContent = suggestion;
        item.style.cssText = `
            padding: 0.5rem;
            cursor: pointer;
            border-bottom: 1px solid var(--bs-border-color, #dee2e6);
        `;
        
        if (index === 0) {
            item.classList.add('selected');
            item.style.backgroundColor = 'var(--bs-primary-bg-subtle, #cfe2ff)';
        }
        
        item.addEventListener('mousedown', (e) => {
            e.preventDefault(); // Prevent textarea blur
            selectSuggestion(suggestion);
        });
        
        item.addEventListener('mouseover', () => {
            currentSuggestionIndex = index;
            updateSuggestionSelection();
        });
        
        suggestionBox.appendChild(item);
    });
    
    // Position the suggestion box
    positionSuggestionBox(textarea, suggestionBox);
    
    // Add to DOM right after the textarea
    textarea.parentNode.insertBefore(suggestionBox, textarea.nextSibling);
    
    // Ensure the parent has relative positioning
    const parent = textarea.parentNode;
    if (getComputedStyle(parent).position === 'static') {
        parent.style.position = 'relative';
    }
    
    activeSuggestionBox = suggestionBox;
}

function positionSuggestionBox(textarea, suggestionBox) {
    const cursorPos = getCursorPosition(textarea);
    const style = getComputedStyle(textarea);
    
    // Account for textarea's padding and border
    const paddingLeft = parseFloat(style.paddingLeft) || 0;
    const paddingTop = parseFloat(style.paddingTop) || 0;
    const borderLeft = parseFloat(style.borderLeftWidth) || 0;
    const borderTop = parseFloat(style.borderTopWidth) || 0;
    
    // Position relative to textarea using CSS positioning
    suggestionBox.style.position = 'absolute';
    suggestionBox.style.left = (textarea.offsetLeft + paddingLeft + borderLeft + cursorPos.left) + 'px';
    suggestionBox.style.top = (textarea.offsetTop + paddingTop + borderTop + cursorPos.top + 20) + 'px';
}


function getCursorPosition(textarea) {
    // Create a mirror div to calculate cursor position
    const mirrorDiv = document.createElement('div');
    const style = getComputedStyle(textarea);
    
    // Copy all relevant styles from textarea to mirror div
    const relevantStyles = [
        'font-family', 'font-size', 'font-weight', 'font-style', 'letter-spacing',
        'text-transform', 'word-spacing', 'text-indent', 'text-decoration',
        'box-sizing', 'border-left-width', 'border-right-width', 'border-top-width', 'border-bottom-width',
        'padding-left', 'padding-right', 'padding-top', 'padding-bottom',
        'margin-left', 'margin-right', 'margin-top', 'margin-bottom',
        'line-height', 'white-space', 'word-wrap', 'overflow-wrap'
    ];
    
    relevantStyles.forEach(prop => {
        mirrorDiv.style[prop] = style[prop];
    });
    
    // Set mirror div properties
    mirrorDiv.style.position = 'absolute';
    mirrorDiv.style.visibility = 'hidden';
    mirrorDiv.style.whiteSpace = 'pre-wrap';
    mirrorDiv.style.wordWrap = 'break-word';
    mirrorDiv.style.top = '0';
    mirrorDiv.style.left = '0';
    mirrorDiv.style.width = textarea.offsetWidth + 'px';
    mirrorDiv.style.height = 'auto';
    mirrorDiv.style.overflow = 'hidden';
    
    // Get text before cursor
    const textBeforeCursor = textarea.value.substring(0, textarea.selectionStart);
    
    // Add text before cursor to mirror div
    mirrorDiv.textContent = textBeforeCursor;
    
    // Add a span to mark cursor position
    const cursorSpan = document.createElement('span');
    cursorSpan.textContent = '|'; // Dummy character to measure position
    mirrorDiv.appendChild(cursorSpan);
    
    // Add mirror to DOM temporarily
    document.body.appendChild(mirrorDiv);
    
    // Get cursor position
    const cursorRect = cursorSpan.getBoundingClientRect();
    const mirrorRect = mirrorDiv.getBoundingClientRect();
    
    // Calculate position relative to the mirror div's content area
    const left = cursorRect.left - mirrorRect.left;
    const top = cursorRect.top - mirrorRect.top;
    
    // Remove mirror div
    document.body.removeChild(mirrorDiv);
    
    return { left, top };
}

function updateSuggestionSelection() {
    if (!activeSuggestionBox) return;
    
    const items = activeSuggestionBox.querySelectorAll('.user-suggestion-item');
    items.forEach((item, index) => {
        if (index === currentSuggestionIndex) {
            item.classList.add('selected');
            item.style.backgroundColor = 'var(--bs-primary-bg-subtle, #cfe2ff)';
        } else {
            item.classList.remove('selected');
            item.style.backgroundColor = '';
        }
    });
}

function selectSuggestion(suggestion) {
    if (!activeSuggestionTextarea) return;
    
    const textarea = activeSuggestionTextarea;
    const cursorPos = textarea.selectionStart;
    const textBeforeCursor = textarea.value.substring(0, cursorPos);
    const textAfterCursor = textarea.value.substring(cursorPos);
    
    // Find the @ or ! or ~ symbol and replace the partial mention with the full suggestion
    const mentionMatch = textBeforeCursor.match(/(?:^|\s|[\r\n])[@!~]([a-zA-Z0-9_.-]*?)$/);
    if (mentionMatch) {
        const fullMatch = mentionMatch[0];
        const trigger = fullMatch.match(/[@!~]/)[0]; // Extract @ or ! or ~
        const beforeTrigger = textBeforeCursor.substring(0, mentionMatch.index + fullMatch.indexOf(trigger));
        const newValue = beforeTrigger + trigger + suggestion + ' ' + textAfterCursor;
        
        textarea.value = newValue;
        const newCursorPos = beforeTrigger.length + 1 + suggestion.length + 1; // +1 for trigger, +1 for space
        textarea.setSelectionRange(newCursorPos, newCursorPos);
        
        // Trigger input event to ensure any other listeners are notified
        textarea.dispatchEvent(new Event('input', { bubbles: true }));
    }
    
    hideUserSuggestions();
    textarea.focus();
}

function hideUserSuggestions() {
    if (activeSuggestionBox) {
        activeSuggestionBox.remove();
        activeSuggestionBox = null;
    }
    activeSuggestionTextarea = null;
    currentSuggestions = [];
    currentSuggestionIndex = -1;
}