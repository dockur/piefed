/**
 * Helper functions for admin reports to reduce code duplication
 */

/**
 * Get CSRF token from various sources
 * @returns {string} CSRF token value
 */
function getCSRFToken() {
    return document.querySelector('meta[name="csrf-token"]')?.getAttribute('content') || 
           document.querySelector('input[name="csrf_token"]')?.value || '';
}

/**
 * Set button loading state
 * @param {HTMLElement} button - Button element
 * @param {boolean} loading - Whether button is loading
 * @param {string} loadingText - Text to show when loading
 */
function setButtonLoadingState(button, loading, loadingText = 'Processing...') {
    if (!button) return;
    
    button.disabled = loading;
    
    if (loading) {
        button.dataset.originalText = button.innerHTML;
        button.innerHTML = `<span class="spinner-border spinner-border-sm me-1"></span>${loadingText}`;
    } else if (button.dataset.originalText) {
        button.innerHTML = button.dataset.originalText;
        delete button.dataset.originalText;
    }
}

/**
 * Create and show a Bootstrap modal
 * @param {Object} config - Modal configuration
 * @returns {bootstrap.Modal} Modal instance
 */
function createActionModal(config) {
    const {
        id = 'actionModal',
        title = 'Action',
        body = '',
        footer = '',
        onHidden = null
    } = config;
    
    // Remove existing modal if any
    const existingModal = document.getElementById(id);
    if (existingModal) {
        existingModal.remove();
    }
    
    // Create modal HTML
    const modalHtml = `
        <div class="modal fade" id="${id}" tabindex="-1">
            <div class="modal-dialog">
                <div class="modal-content">
                    <div class="modal-header">
                        <h5 class="modal-title">${title}</h5>
                        <button type="button" class="btn-close" data-bs-dismiss="modal"></button>
                    </div>
                    <div class="modal-body">
                        ${body}
                    </div>
                    <div class="modal-footer">
                        ${footer}
                    </div>
                </div>
            </div>
        </div>
    `;
    
    // Add modal to body
    document.body.insertAdjacentHTML('beforeend', modalHtml);
    
    // Get modal element
    const modalElement = document.getElementById(id);
    
    // Add hidden event listener
    if (onHidden) {
        modalElement.addEventListener('hidden.bs.modal', onHidden);
    } else {
        modalElement.addEventListener('hidden.bs.modal', function() {
            this.remove();
        });
    }
    
    // Create and show modal
    const modal = new bootstrap.Modal(modalElement);
    modal.show();
    
    return modal;
}

/**
 * Build modal body for report actions
 * @param {string} action - Action type
 * @param {Object} data - Additional data for the modal
 * @returns {string} HTML for modal body
 */
function buildReportActionModalBody(action, data = {}) {
    const t = window.reportTranslations || {};
    const requiresReason = ['ban_user', 'remove_content'];
    
    let bodyHtml = '';
    
    // Username display
    if (data.username) {
        bodyHtml += `
            <div class="mb-3">
                <label class="form-label">${t.user || 'User:'}</label>
                <div class="fw-bold">${data.username}</div>
            </div>
        `;
    }
    
    // Ban-specific fields
    if (action === 'ban_user') {
        bodyHtml += `
            <div class="mb-3">
                <label class="form-label">${t.banDuration || 'Ban Duration:'}</label>
                <select class="form-select" id="banDuration">
                    <option value="0" ${data.ban_duration === '0' ? 'selected' : ''}>${t.permanent || 'Permanent'}</option>
                    <option value="24" ${data.ban_duration === '24' ? 'selected' : ''}>${t.hours24 || '24 hours'}</option>
                    <option value="168" ${data.ban_duration === '168' ? 'selected' : ''}>${t.week1 || '1 week'}</option>
                    <option value="720" ${data.ban_duration === '720' ? 'selected' : ''}>${t.days30 || '30 days'}</option>
                </select>
            </div>
            <div class="mb-3">
                <label class="form-label">${t.banScope || 'Ban Scope:'}</label>
                <select class="form-select" id="banScope">
                    <option value="community" ${data.ban_scope === 'community' ? 'selected' : ''}>${t.communityOnly || 'Community only'}</option>
                    <option value="site" ${data.ban_scope === 'site' ? 'selected' : ''}>${t.siteWide || 'Site-wide'}</option>
                </select>
            </div>
            <div class="form-check mb-3">
                <input class="form-check-input" type="checkbox" id="deleteContent" ${data.deleteContent !== false ? 'checked' : ''}>
                <label class="form-check-label" for="deleteContent">
                    ${t.deleteAllContent || 'Delete all content from this user'}
                </label>
            </div>
            <p class="text-muted small mb-0">
                <i class="fe fe-info"></i> ${t.banningUserNote || 'Banning a user will automatically resolve all reports against them.'}
            </p>
        `;
    }
    
    // Reason field
    bodyHtml += `
        <div class="mb-3">
            <label class="form-label">${requiresReason.includes(action) ? (t.reasonRequired || 'Reason (required):') : (t.reasonOptional || 'Reason (optional):')}</label>
            <textarea class="form-control" id="reasonText" rows="3" ${requiresReason.includes(action) ? 'required' : ''}></textarea>
        </div>
    `;
    
    // Resolve similar checkbox
    if (['resolve', 'remove_content'].includes(action)) {
        bodyHtml += `
            <div class="form-check">
                <input class="form-check-input" type="checkbox" id="resolveSimilar" ${data.resolveSimilar !== false ? 'checked' : ''}>
                <label class="form-check-label" for="resolveSimilar">
                    ${t.resolveSimilarReports || 'Resolve similar reports on the same content/user'}
                </label>
            </div>
        `;
    }
    
    // Info notes
    if (action === 'remove_content') {
        bodyHtml += `
            <p class="text-muted small mt-2 mb-0">
                <i class="fe fe-info"></i> ${t.removingContentNote || 'Removing content will automatically resolve all reports for that content.'}
            </p>
        `;
    }
    
    return bodyHtml;
}

/**
 * Submit report action via API
 * @param {string} endpoint - API endpoint
 * @param {Object} data - Request data
 * @returns {Promise} API response promise
 */
function submitReportAction(endpoint, data) {
    return fetch(endpoint, {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            'X-CSRFToken': getCSRFToken()
        },
        body: JSON.stringify(data),
        credentials: 'same-origin'
    }).then(response => response.json());
}

/**
 * Extract modal form data
 * @param {string} action - Action type
 * @returns {Object} Form data
 */
function extractModalFormData(action) {
    const data = {
        reason: document.getElementById('reasonText')?.value.trim() || ''
    };
    
    if (action === 'ban_user') {
        data.ban_duration = document.getElementById('banDuration')?.value || '0';
        data.ban_scope = document.getElementById('banScope')?.value || 'community';
        data.delete_content = document.getElementById('deleteContent')?.checked || false;
    }
    
    if (document.getElementById('resolveSimilar')) {
        data.resolve_similar = document.getElementById('resolveSimilar').checked;
    }
    
    return data;
}

/**
 * Validate form data
 * @param {string} action - Action type
 * @param {Object} data - Form data
 * @returns {boolean} Whether data is valid
 */
function validateActionData(action, data) {
    const requiresReason = ['ban_user', 'remove_content'];
    
    if (requiresReason.includes(action) && !data.reason) {
        const reasonField = document.getElementById('reasonText');
        if (reasonField) {
            reasonField.classList.add('is-invalid');
        }
        return false;
    }
    
    return true;
}

/**
 * Get action button original text
 * @param {string} actionType - Type of action
 * @returns {string} Button text with icon
 */
function getActionButtonText(actionType) {
    const texts = {
        'resolve': '<span class="fe fe-check"></span> Resolve',
        'dismiss': '<span class="fe fe-x"></span> Dismiss',
        'remove_content': '<span class="fe fe-trash-2"></span> Remove',
        'ban_user': '<span class="fe fe-user-x"></span> Ban'
    };
    
    return texts[actionType] || actionType;
}

/**
 * Create toast notification container if not exists
 * @returns {HTMLElement} Toast container element
 */
function getOrCreateToastContainer() {
    let container = document.getElementById('toast-container');
    if (!container) {
        container = document.createElement('div');
        container.id = 'toast-container';
        container.className = 'toast-container position-fixed bottom-0 end-0 p-3';
        container.style.zIndex = '1050';
        document.body.appendChild(container);
    }
    return container;
}

// Export functions for use in main script
window.reportHelpers = {
    setButtonLoadingState,
    createActionModal,
    buildReportActionModalBody,
    submitReportAction,
    extractModalFormData,
    validateActionData,
    getActionButtonText,
    getOrCreateToastContainer
};