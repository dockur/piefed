
let bulkMode = false;
let selectedReports = [];

function toggleCommentExpansion(event, link) {
    event.preventDefault();
    event.stopPropagation();
    
    const wrapper = link.closest('.comment-body-wrapper');
    
    if (!wrapper) {
        return false;
    }
    
    const collapsed = wrapper.querySelector('.comment-body-collapsed');
    const full = wrapper.querySelector('.comment-body-full');
    const icon = link.querySelector('.fe');
    const textSpan = link.querySelector('span:last-child');
    
    
    if (!collapsed || !full) {
        return false;
    }
    
    if (full.classList.contains('d-none')) {
        collapsed.classList.add('d-none');
        full.classList.remove('d-none');
        if (icon) {
            icon.classList.remove('fe-chevron-down');
            icon.classList.add('fe-chevron-up');
        }
        if (textSpan) textSpan.textContent = window.reportTranslations?.showLess || 'Show less';
    } else {
        collapsed.classList.remove('d-none');
        full.classList.add('d-none');
        if (icon) {
            icon.classList.remove('fe-chevron-up');
            icon.classList.add('fe-chevron-down');
        }
        if (textSpan) textSpan.textContent = window.reportTranslations?.showMore || 'Show more';
    }
    
    return false;
}

window.toggleCommentExpansion = toggleCommentExpansion;
window.toggleBulkMode = toggleBulkMode;
window.toggleFilters = toggleFilters;
window.quickReportAction = quickReportAction;
window.openBanModal = openBanModal;

document.addEventListener('DOMContentLoaded', function() {
    initializeReportsInterface();
    initializeModalHandlers();
    initializeQuickActions();
    initializeMultiSelect();
    
    document.addEventListener('click', function(e) {
        const target = e.target.closest('[data-action]');
        if (!target) return;
        
        e.preventDefault();
        const action = target.dataset.action;
        
        switch(action) {
            case 'toggle-bulk-mode':
                toggleBulkMode();
                break;
            case 'toggle-filters':
                toggleFilters();
                break;
            case 'quick-report-action':
                if (target.disabled || target.classList.contains('disabled')) {
                    return;
                }
                const reportId = parseInt(target.dataset.reportId);
                const actionType = target.dataset.actionType;
                const reportType = target.dataset.reportType;
                quickReportAction(reportId, actionType, reportType);
                break;
            case 'open-ban-modal':
                if (target.disabled || target.classList.contains('disabled')) {
                    return;
                }
                const banReportId = parseInt(target.dataset.reportId);
                const username = target.dataset.username;
                openBanModal(banReportId, username);
                break;
            case 'execute-reason':
                const execReportId = parseInt(target.dataset.reportId);
                const execAction = target.dataset.actionType;
                executeReasonAction(execReportId, execAction);
                break;
            case 'execute-bulk':
                const reportIds = target.dataset.reportIds.split(',');
                const bulkAction = target.dataset.actionType;
                executeBulkAction(reportIds.join(','), bulkAction);
                break;
            case 'view-conversation':
                const conversationReportId = parseInt(target.dataset.reportId);
                viewPrivateConversation(conversationReportId);
                break;
        }
    });
    
    const bulkActionButton = document.getElementById('bulkActionButton');
    if (bulkActionButton) {
        bulkActionButton.addEventListener('click', function(e) {
            e.preventDefault();
            
            const bulkActionSelect = document.getElementById('bulkActionSelect');
            const action = bulkActionSelect.value;
            
            if (!action) {
                alert('Please select an action');
                return;
            }
            
            if (selectedReports.length === 0) {
                alert('Please select at least one report');
                return;
            }
            
            if (action === 'resolve' || action === 'dismiss') {
                executeBulkActionDirect(selectedReports.join(','), action);
            } else {
                if (!confirmBulkAction()) {
                    return;
                }
                showBulkReasonDialog(selectedReports, action);
            }
        });
    }
    
    document.addEventListener('click', function(e) {
        const button = e.target.closest('.expand-comment');
        if (button) {
            e.preventDefault();
            e.stopPropagation();
            toggleCommentExpansion(e, button);
        }
    });
});

function initializeReportsInterface() {
    if (localStorage.getItem('reportsAutoRefresh') === 'true') {
        setInterval(refreshReports, 30000);
    }
    
    var tooltipTriggerList = [].slice.call(document.querySelectorAll('[data-bs-toggle="tooltip"]'));
    var tooltipList = tooltipTriggerList.map(function (tooltipTriggerEl) {
        return new bootstrap.Tooltip(tooltipTriggerEl);
    });
    initializeCollapsibles();
}




function updateBulkActionButton() {
    const bulkActionButton = document.getElementById('bulkActionButton');
    if (bulkActionButton) {
        bulkActionButton.disabled = selectedReports.length === 0;
        bulkActionButton.innerHTML = selectedReports.length > 0 
            ? `<span class="fe fe-zap"></span> Apply to ${selectedReports.length} Selected`
            : '<span class="fe fe-zap"></span> Apply';
    }
}

document.addEventListener('change', function(e) {
    if (e.target.classList.contains('report-checkbox')) {
        updateSelectedCount();
    }
});



function toggleActionFields(action) {
    const banFields = document.getElementById('banFields');
    
    if (action === 'ban_user') {
        banFields.style.display = 'block';
    } else {
        banFields.style.display = 'none';
    }
}

function quickReportAction(reportId, action, reportType) {
    const actionsRequiringReason = ['ban_user', 'remove_content'];
    
    if (actionsRequiringReason.includes(action)) {
        showReasonDialog(reportId, action, { reportType: reportType });
    } else {
        submitQuickAction(reportId, action, '');
    }
}


function submitQuickAction(reportId, action, reason, additionalData = {}) {
    const requestData = {
        report_id: reportId,
        action: action,
        reason: reason,
        notify_reporter: true,
        ban_duration: '0',
        ban_scope: 'community',
        delete_content: false,
        resolve_similar: false,
        ...additionalData
    };
    
    const actionButtons = document.querySelectorAll(`[data-report-id="${reportId}"]`);
    actionButtons.forEach(btn => {
        if (btn.dataset.actionType === action) {
            reportHelpers.setButtonLoadingState(btn, true);
        } else {
            btn.disabled = true;
        }
    });
    
    reportHelpers.submitReportAction('/admin/reports/action', requestData)
        .then(data => {
            if (data.success) {
                updateReportStatus(reportId, action, data);
                showNotification('success', data.message || 'Action completed successfully');
            } else {
                showNotification('error', data.error || 'Action failed');
            }
        })
        .catch(error => {
            console.error('Error:', error);
            showNotification('error', 'An error occurred while processing the action');
        })
        .finally(() => {
            actionButtons.forEach(btn => {
                btn.disabled = false;
                if (btn.dataset.actionType === action) {
                    btn.innerHTML = reportHelpers.getActionButtonText(btn.dataset.actionType);
                }
            });
        });
}



function confirmBulkAction() {
    if (selectedReports.length === 0) {
        alert('Please select at least one report.');
        return false;
    }
    
    const action = document.querySelector('select[name="bulk_action"]').value;
    const actionText = document.querySelector(`select[name="bulk_action"] option[value="${action}"]`).textContent;
    
    return confirm(`Are you sure you want to ${actionText.toLowerCase()} ${selectedReports.length} reports?`);
}

function updateReportStatus(reportId, action, data) {
    const reportRow = document.querySelector(`[data-report-id="${reportId}"]`).closest('.report-item');
    if (!reportRow) return;
    
    const statusBadge = reportRow.querySelector('.status-badge');
    if (statusBadge) {
        if (action === 'resolve') {
            statusBadge.className = 'badge bg-success status-badge';
            statusBadge.textContent = 'Resolved';
        } else if (action === 'dismiss') {
            statusBadge.className = 'badge bg-secondary status-badge';
            statusBadge.textContent = 'Dismissed';
        }
    }
    
    if (action === 'remove_content' && data.content_removed) {
        const contentSection = reportRow.querySelector('.reported-content-highlight');
        if (contentSection) {
            const existingBadges = contentSection.querySelector('.d-flex.align-items-center.mb-1');
            if (existingBadges && !existingBadges.querySelector('.content-deleted-badge')) {
                const deletedBadge = document.createElement('span');
                deletedBadge.className = 'badge bg-warning text-dark ms-2 content-deleted-badge';
                deletedBadge.title = 'Content has been deleted';
                deletedBadge.innerHTML = '🚫 Content Deleted';
                existingBadges.appendChild(deletedBadge);
            }
        }
        
        const statusBadge = reportRow.querySelector('.status-badge');
        if (statusBadge) {
            statusBadge.className = 'badge bg-success status-badge';
            statusBadge.textContent = 'Resolved';
        }
    }
    
    if (action === 'ban_user' && data.user_banned) {
        const userInfo = reportRow.querySelector('.d-flex.align-items-center.mb-1');
        if (userInfo && !userInfo.querySelector('.user-banned-badge')) {
            const bannedBadge = document.createElement('span');
            bannedBadge.className = 'badge bg-danger ms-2 user-banned-badge';
            bannedBadge.title = 'User is banned';
            bannedBadge.innerHTML = '🚫 Banned';
            userInfo.appendChild(bannedBadge);
        }
        
        const statusBadge = reportRow.querySelector('.status-badge');
        if (statusBadge) {
            statusBadge.className = 'badge bg-success status-badge';
            statusBadge.textContent = 'Resolved';
        }
    }
    
    reportRow.style.opacity = '0.7';
}

function showNotification(type, message) {
    const toastHtml = `
        <div class="toast align-items-center text-white bg-${type === 'success' ? 'success' : 'danger'} border-0" role="alert" aria-live="assertive" aria-atomic="true">
            <div class="d-flex">
                <div class="toast-body">
                    ${message}
                </div>
                <button type="button" class="btn-close btn-close-white me-2 m-auto" data-bs-dismiss="toast" aria-label="Close"></button>
            </div>
        </div>
    `;
    
    const toastContainer = reportHelpers.getOrCreateToastContainer();
    
    const toastElement = document.createElement('div');
    toastElement.innerHTML = toastHtml;
    const toast = toastElement.firstElementChild;
    toastContainer.appendChild(toast);
    
    const bsToast = new bootstrap.Toast(toast, { delay: 3000 });
    bsToast.show();
    
    toast.addEventListener('hidden.bs.toast', () => {
        toast.remove();
    });
}

function refreshReports() {
    window.location.reload();
}

function initializeModalHandlers() {
    const actionSelect = document.querySelector('select[name="action"]');
    if (actionSelect) {
        actionSelect.addEventListener('change', function() {
            toggleActionFields(this.value);
        });
    }
}

function initializeQuickActions() {
    document.addEventListener('keydown', function(e) {
        if (document.activeElement.tagName !== 'INPUT' && 
            document.activeElement.tagName !== 'TEXTAREA') {
            
            switch(e.key) {
                case 'b':
                    if (e.ctrlKey || e.metaKey) {
                        e.preventDefault();
                        toggleBulkMode();
                    }
                    break;
                case 'r':
                    if (e.ctrlKey || e.metaKey) {
                        e.preventDefault();
                        refreshReports();
                    }
                    break;
            }
        }
    });
}

function initializeMultiSelect() {
    const selectAllCheckbox = document.getElementById('select-all-reports');
    if (selectAllCheckbox) {
        selectAllCheckbox.addEventListener('change', function() {
            const checkboxes = document.querySelectorAll('.report-select');
            checkboxes.forEach(cb => {
                cb.checked = this.checked;
                updateReportItemSelection(cb);
            });
            updateSelectedCount();
        });
    }
    
    document.addEventListener('change', function(e) {
        if (e.target.classList.contains('report-select')) {
            updateReportItemSelection(e.target);
            updateSelectedCount();
            updateSelectAllState();
        }
    });
    
    document.addEventListener('click', function(e) {
        if (!bulkMode) return;
        
        const reportItem = e.target.closest('.report-item');
        const reportMetadata = e.target.closest('.report-metadata');
        
        if ((reportItem || reportMetadata) && !e.target.closest('a, button, input, label')) {
            const parentItem = reportItem || reportMetadata.closest('.report-item');
            const checkbox = parentItem.querySelector('.report-select');
            if (checkbox) {
                checkbox.checked = !checkbox.checked;
                updateReportItemSelection(checkbox);
                updateSelectedCount();
                updateSelectAllState();
            }
        }
    });
}

function updateReportItemSelection(checkbox) {
    const reportItem = checkbox.closest('.report-item');
    if (reportItem) {
        if (checkbox.checked) {
            reportItem.classList.add('selected');
        } else {
            reportItem.classList.remove('selected');
        }
    }
}

function updateSelectedCount() {
    const checkedBoxes = document.querySelectorAll('.report-select:checked');
    const count = checkedBoxes.length;
    
    const countDisplay = document.getElementById('selected-count');
    if (countDisplay) {
        countDisplay.textContent = `(${window.reportTranslations?.selectedCount?.replace('%s', count) || count + ' selected'})`;
    }
    
    const bulkCountDisplay = document.querySelector('.selected-count');
    if (bulkCountDisplay) {
        bulkCountDisplay.textContent = window.reportTranslations?.selectedCount?.replace('%s', count) || `${count} selected`;
    }
    
    const bulkActionsBar = document.getElementById('bulkActionsBar');
    if (bulkActionsBar && count > 0) {
        bulkActionsBar.style.display = 'block';
    } else if (bulkActionsBar && count === 0) {
        bulkActionsBar.style.display = 'none';
    }
    
    const reportIds = Array.from(checkedBoxes).map(cb => cb.value);
    selectedReports = reportIds;
    updateBulkActionButton();
}

function updateSelectAllState() {
    const selectAll = document.getElementById('select-all-reports');
    const checkboxes = document.querySelectorAll('.report-select');
    const checkedBoxes = document.querySelectorAll('.report-select:checked');
    
    if (selectAll) {
        if (checkedBoxes.length === 0) {
            selectAll.checked = false;
            selectAll.indeterminate = false;
        } else if (checkedBoxes.length === checkboxes.length) {
            selectAll.checked = true;
            selectAll.indeterminate = false;
        } else {
            selectAll.checked = false;
            selectAll.indeterminate = true;
        }
    }
}





function initializeCollapsibles() {
    document.querySelectorAll('[data-bs-toggle="collapse"]').forEach(function(element) {
        element.addEventListener('click', function() {
            const icon = this.querySelector('.fa-chevron-right');
            if (icon) {
                icon.style.transform = this.getAttribute('aria-expanded') === 'true' 
                    ? 'rotate(90deg)' 
                    : 'rotate(0)';
            }
        });
    });
}

function toggleFilters() {
    const filtersSection = document.getElementById('filtersSection');
    filtersSection.classList.toggle('collapsed');
}

function toggleBulkMode() {
    bulkMode = !bulkMode;
    
    const bulkActionsBar = document.getElementById('bulkActionsBar');
    const selectContainers = document.querySelectorAll('.report-select-container');
    const bulkButton = document.querySelector('[data-action="toggle-bulk-mode"]');
    const reportsContainer = document.querySelector('.admin-reports-container');
    
    if (bulkMode) {
        selectContainers.forEach(container => container.style.display = 'block');
        if (bulkActionsBar) bulkActionsBar.style.display = 'block';
        if (reportsContainer) reportsContainer.classList.add('bulk-mode-active');
        
        if (bulkButton) {
            bulkButton.innerHTML = '<span class="fe fe-x"></span> Cancel';
            bulkButton.classList.remove('btn-primary');
            bulkButton.classList.add('btn-secondary');
        }
    } else {
        selectContainers.forEach(container => container.style.display = 'none');
        if (bulkActionsBar) bulkActionsBar.style.display = 'none';
        if (reportsContainer) reportsContainer.classList.remove('bulk-mode-active');
        
        document.querySelectorAll('.report-select').forEach(cb => {
            cb.checked = false;
            updateReportItemSelection(cb);
        });
        
        const selectAll = document.getElementById('select-all-reports');
        if (selectAll) selectAll.checked = false;
        
        updateSelectedCount();
        updateSelectAllState();
        
        if (bulkButton) {
            bulkButton.innerHTML = '<span class="fe fe-zap"></span> ' + (window.reportTranslations?.bulkActions || 'Bulk Actions');
            bulkButton.classList.remove('btn-secondary');
            bulkButton.classList.add('btn-primary');
        }
    }
}


function viewPrivateConversation(reportId) {
    if (confirm('This is a private conversation. Only view if necessary for moderation. Continue?')) {
        window.open(`/admin/reports/${reportId}/conversation`, '_blank');
    }
}

function openBanModal(reportId, username) {
    showReasonDialog(reportId, 'ban_user', { username: username });
}

function showReasonDialog(reportId, action, additionalData = {}) {
    const t = window.reportTranslations || {};
    let actionTitles = {
        'ban_user': t.banUser || 'Ban User',
        'remove_content': t.removeContent || 'Remove Content',
        'resolve': t.resolveReport || 'Resolve Report',
        'dismiss': t.dismissReport || 'Dismiss Report'
    };
    
    if (action === 'remove_content' && additionalData.reportType) {
        if (additionalData.reportType === '1') {
            actionTitles['remove_content'] = t.removePost || 'Remove Post';
        } else if (additionalData.reportType === '2') {
            actionTitles['remove_content'] = t.removeComment || 'Remove Comment';
        }
    }
    
    const modalBody = reportHelpers.buildReportActionModalBody(action, additionalData);
    
    const modalFooter = `
        <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">${t.cancel || 'Cancel'}</button>
        <button type="button" class="btn btn-danger" data-action="execute-reason" data-report-id="${reportId}" data-action-type="${action}">
            ${actionTitles[action] || t.confirm || 'Confirm'}
        </button>
    `;
    
    reportHelpers.createActionModal({
        id: 'reasonModal',
        title: actionTitles[action] || action,
        body: modalBody,
        footer: modalFooter
    });
}

function executeReasonAction(reportId, action) {
    const formData = reportHelpers.extractModalFormData(action);
    
    if (!reportHelpers.validateActionData(action, formData)) {
        return;
    }
    
    const modal = bootstrap.Modal.getInstance(document.getElementById('reasonModal'));
    modal.hide();
    
    submitQuickAction(reportId, action, formData.reason, formData);
}

function showBulkReasonDialog(reportIds, action) {
    const t = window.reportTranslations || {};
    const actionTitles = {
        'resolve': t.resolveReports || 'Resolve Reports',
        'dismiss': t.dismissReports || 'Dismiss Reports',
        'remove_content': t.removeContent || 'Remove Content',
        'ban_user': t.banUsers || 'Ban Users'
    };
    
    const requiresReason = ['ban_user', 'remove_content'];
    
    const modalHtml = `
        <div class="modal fade" id="bulkReasonModal" tabindex="-1">
            <div class="modal-dialog">
                <div class="modal-content">
                    <div class="modal-header">
                        <h5 class="modal-title">${actionTitles[action] || action} (${t.reportCount?.replace('%s', reportIds.length) || reportIds.length + ' reports'})</h5>
                        <button type="button" class="btn-close" data-bs-dismiss="modal"></button>
                    </div>
                    <div class="modal-body">
                        ${action === 'ban_user' ? `
                            <div class="mb-3">
                                <label class="form-label">${t.banDuration || 'Ban Duration:'}</label>
                                <select class="form-select" id="bulkBanDuration">
                                    <option value="0">${t.permanent || 'Permanent'}</option>
                                    <option value="24">${t.hours24 || '24 hours'}</option>
                                    <option value="168">${t.week1 || '1 week'}</option>
                                    <option value="720">${t.days30 || '30 days'}</option>
                                </select>
                            </div>
                            <div class="mb-3">
                                <label class="form-label">${t.banScope || 'Ban Scope:'}</label>
                                <select class="form-select" id="bulkBanScope">
                                    <option value="community">${t.communityOnly || 'Community only'}</option>
                                    <option value="site">${t.siteWide || 'Site-wide'}</option>
                                </select>
                            </div>
                            <div class="form-check mb-3">
                                <input class="form-check-input" type="checkbox" id="bulkDeleteContent" checked>
                                <label class="form-check-label" for="bulkDeleteContent">
                                    ${t.deleteAllContent || 'Delete all content from banned users'}
                                </label>
                            </div>
                            <p class="text-muted small mb-3">
                                <i class="fe fe-info"></i> ${t.banningUserNote || 'Banning users will automatically resolve all reports against them.'}
                            </p>
                        ` : ''}
                        ${action === 'remove_content' ? `
                            <p class="text-muted small mb-3">
                                <i class="fe fe-info"></i> ${t.removingContentNote || 'Removing content will automatically resolve all reports for that content.'}
                            </p>
                        ` : ''}
                        <div class="mb-3">
                            <label class="form-label">${requiresReason.includes(action) ? (t.reasonRequired || 'Reason (required):') : (t.reasonOptional || 'Reason (optional):')}</label>
                            <textarea class="form-control" id="bulkReasonText" rows="3" ${requiresReason.includes(action) ? 'required' : ''}></textarea>
                        </div>
                    </div>
                    <div class="modal-footer">
                        <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">Cancel</button>
                        <button type="button" class="btn btn-primary" data-action="execute-bulk" data-report-ids="${reportIds.join(',')}" data-action-type="${action}">
                            ${actionTitles[action] || 'Confirm'}
                        </button>
                    </div>
                </div>
            </div>
        </div>
    `;
    
    const existingModal = document.getElementById('bulkReasonModal');
    if (existingModal) {
        existingModal.remove();
    }
    
    document.body.insertAdjacentHTML('beforeend', modalHtml);
    
    const modal = new bootstrap.Modal(document.getElementById('bulkReasonModal'));
    modal.show();
    
    document.getElementById('bulkReasonModal').addEventListener('hidden.bs.modal', function() {
        this.remove();
    });
}

function executeBulkActionDirect(reportIdsStr, action) {
    const requestData = {
        report_ids: reportIdsStr.split(','),
        action: action,
        reason: ''
    };
    
    reportHelpers.submitReportAction('/admin/reports/bulk-action', requestData)
        .then(data => {
            if (data.success) {
                showNotification('success', data.message || `Successfully ${action}d ${reportIdsStr.split(',').length} reports`);
                
                reportIdsStr.split(',').forEach(reportId => {
                    updateReportStatus(reportId, action, data);
                });
                
                document.querySelectorAll('.report-select:checked').forEach(cb => {
                    cb.checked = false;
                });
                updateSelectedCount();
                
                bulkMode = false;
                toggleBulkMode();
            } else {
                showNotification('error', data.error || 'Action failed');
            }
        })
        .catch(error => {
            console.error('Error:', error);
            showNotification('error', 'An error occurred');
        });
}

function executeBulkAction(reportIdsStr, action) {
    const reason = document.getElementById('bulkReasonText').value.trim();
    const requiresReason = ['ban_user', 'remove_content'];
    
    if (requiresReason.includes(action) && !reason) {
        document.getElementById('bulkReasonText').classList.add('is-invalid');
        return;
    }
    
    const requestData = {
        report_ids: reportIdsStr.split(','),
        action: action,
        reason: reason
    };
    
    if (action === 'ban_user') {
        requestData.ban_duration = document.getElementById('bulkBanDuration').value;
        requestData.ban_scope = document.getElementById('bulkBanScope').value;
        requestData.delete_content = document.getElementById('bulkDeleteContent').checked;
    }
    
    const modal = bootstrap.Modal.getInstance(document.getElementById('bulkReasonModal'));
    modal.hide();
    
    reportHelpers.submitReportAction('/admin/reports/bulk-action', requestData)
        .then(data => {
            if (data.success) {
                showNotification('success', data.message);
                requestData.report_ids.forEach(reportId => {
                    updateReportStatus(reportId, action, {});
                });
                toggleBulkMode();
            } else {
                showNotification('error', data.error || 'Bulk action failed');
            }
        })
        .catch(error => {
            console.error('Error:', error);
            showNotification('error', 'An error occurred while processing bulk action');
        });
}

