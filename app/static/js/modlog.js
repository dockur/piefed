
document.addEventListener('DOMContentLoaded', function() {
    const toggleFiltersBtn = document.getElementById('toggle-filters-btn');
    if (toggleFiltersBtn) {
        toggleFiltersBtn.addEventListener('click', function() {
            const filtersSection = document.getElementById('filtersSection');
            filtersSection.classList.toggle('collapsed');
        });
    }
    
    const contentToggleBtns = document.querySelectorAll('.toggle-content-btn');
    contentToggleBtns.forEach(btn => {
        btn.addEventListener('click', async function() {
            const modlogId = this.dataset.modlogId;
            await toggleRemovedContent(modlogId);
        });
    });
});

async function toggleRemovedContent(modlogId) {
    const container = document.getElementById(`removed-content-${modlogId}`);
    const button = document.getElementById(`toggle-content-${modlogId}`);
    const buttonText = button.querySelector('.button-text');
    
    if (container.style.display === 'none') {
        container.style.display = 'block';
        buttonText.textContent = buttonText.dataset.hideText || 'Hide Removed Content';
        
        if (!container.dataset.loaded) {
            try {
                const response = await fetch(`/api/modlog/${modlogId}/removed-content`);
                if (response.ok) {
                    const data = await response.json();
                    container.innerHTML = formatRemovedContent(data);
                    container.dataset.loaded = 'true';
                } else {
                    container.innerHTML = `
                        <div class="alert alert-warning">
                            <i class="fe fe-alert-triangle"></i> Unable to load removed content
                        </div>
                    `;
                }
            } catch (error) {
                container.innerHTML = `
                    <div class="alert alert-danger">
                        <i class="fe fe-x-circle"></i> Error loading content
                    </div>
                `;
            }
        }
    } else {
        container.style.display = 'none';
        buttonText.textContent = buttonText.dataset.viewText || 'View Removed Content';
    }
}

function formatRemovedContent(data) {
    if (data.type === 'post') {
        return `<div class="removed-content-display p-3 rounded bg-light border">
    <h6 class="mb-2 text-danger">
        <i class="fe fe-trash-2"></i> Removed Post Content
    </h6>
    <div class="mb-2">
        <strong>Title:</strong> ${escapeHtml(data.title || '[No title]')}
    </div>
    ${data.body ? `<div class="mb-2">
        <strong>Body:</strong>
        <div class="mt-1 p-2 removed-content-body rounded border">${escapeHtml(data.body.trim())}</div>
    </div>` : ''}
    ${data.url ? `<div class="mb-2">
        <strong>URL:</strong> 
        <a href="${escapeHtml(data.url)}" target="_blank" rel="noopener">
            ${escapeHtml(data.url)}
        </a>
    </div>` : ''}
    <div class="text-muted small mt-2">
        <i class="fe fe-clock"></i> Posted: ${data.created_at || 'Unknown'}
        ${data.author ? `• <i class="fe fe-user"></i> ${escapeHtml(data.author)}` : ''}
    </div>
</div>`;
    } else if (data.type === 'comment') {
        return `<div class="removed-content-display p-3 rounded bg-light border">
    <h6 class="mb-2 text-danger">
        <i class="fe fe-trash-2"></i> Removed Comment Content
    </h6>
    <div class="mb-2">
        <div class="p-2 removed-content-body rounded border">${escapeHtml((data.body || '[No content]').trim())}</div>
    </div>
    ${data.parent_context ? `<div class="mt-3">
        <small class="text-muted d-block mb-1">In reply to:</small>
        <div class="p-2 removed-content-body rounded border">${escapeHtml(data.parent_context.trim())}</div>
    </div>` : ''}
    <div class="text-muted small mt-2">
        <i class="fe fe-clock"></i> Posted: ${data.created_at || 'Unknown'}
        ${data.author ? `• <i class="fe fe-user"></i> ${escapeHtml(data.author)}` : ''}
    </div>
</div>`;
    }
    return '<div class="alert alert-info">Unknown content type</div>';
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}