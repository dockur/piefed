/**
 * Multi-select community chip box for reports page
 * Based on modlog-communities.js
 */

class ReportsCommunityMultiSelect {
    constructor(inputElement) {
        this.input = inputElement;
        this.selectedCommunities = new Map();
        this.searchTimeout = null;
        this.init();
    }

    init() {
        // Create the UI structure
        this.createUI();
        
        // Load initial communities from input value
        this.loadInitialCommunities();
        
        // Set up event listeners
        this.setupEventListeners();
    }

    createUI() {
        // Hide original input
        this.input.style.display = 'none';
        
        // Create container
        this.container = document.createElement('div');
        this.container.className = 'community-multiselect-container';
        this.input.parentNode.insertBefore(this.container, this.input.nextSibling);
        
        // Create chips container (which will also contain the search input)
        this.chipsContainer = document.createElement('div');
        this.chipsContainer.className = 'community-chips';
        this.container.appendChild(this.chipsContainer);
        
        // Create search wrapper inside chips container
        this.searchWrapper = document.createElement('div');
        this.searchWrapper.className = 'community-search-wrapper';
        
        // Create search input
        this.searchInput = document.createElement('input');
        this.searchInput.type = 'text';
        this.searchInput.className = 'community-search-input';
        this.searchInput.placeholder = 'Search communities...';
        this.searchInput.autocomplete = 'off';
        
        this.searchWrapper.appendChild(this.searchInput);
        this.chipsContainer.appendChild(this.searchWrapper);
        
        // Create dropdown for search results
        this.dropdown = document.createElement('div');
        this.dropdown.className = 'community-dropdown';
        this.dropdown.style.display = 'none';
        this.container.appendChild(this.dropdown);
        
        // Click on chips container focuses the search input
        this.chipsContainer.addEventListener('click', (e) => {
            if (e.target === this.chipsContainer) {
                this.searchInput.focus();
            }
        });
    }

    loadInitialCommunities() {
        const value = this.input.value.trim();
        if (value) {
            // If we have initial community IDs, we should fetch their details
            const communityIds = value.split(',').map(id => id.trim()).filter(id => id);
            if (communityIds.length > 0) {
                this.fetchCommunityDetails(communityIds);
            }
        }
    }

    setupEventListeners() {
        // Search input
        this.searchInput.addEventListener('input', (e) => {
            clearTimeout(this.searchTimeout);
            const query = e.target.value.trim();
            
            if (query.length >= 2) {
                this.searchTimeout = setTimeout(() => {
                    this.searchCommunities(query);
                }, 300);
            } else {
                this.hideDropdown();
            }
        });

        // Focus/blur for dropdown
        this.searchInput.addEventListener('focus', () => {
            if (this.searchInput.value.trim().length >= 2) {
                this.showDropdown();
            }
        });

        // Click outside to close dropdown
        document.addEventListener('click', (e) => {
            if (!this.container.contains(e.target)) {
                this.hideDropdown();
            }
        });

        // Keyboard navigation
        this.searchInput.addEventListener('keydown', (e) => {
            if (e.key === 'Escape') {
                this.hideDropdown();
                this.searchInput.blur();
            } else if (e.key === 'Backspace' && this.searchInput.value === '' && this.selectedCommunities.size > 0) {
                // Remove last chip if backspace on empty input
                const lastChip = Array.from(this.selectedCommunities.keys()).pop();
                if (lastChip) {
                    this.removeCommunity(lastChip);
                }
            }
        });
    }

    async searchCommunities(query) {
        try {
            const response = await fetch(`/api/search/communities?q=${encodeURIComponent(query)}&limit=10`);
            if (response.ok) {
                const communities = await response.json();
                this.showSearchResults(communities);
            }
        } catch (error) {
            console.error('Error searching communities:', error);
        }
    }

    async fetchCommunityDetails(communityIds) {
        try {
            const response = await fetch('/api/communities/details', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({ ids: communityIds })
            });
            
            if (response.ok) {
                const communities = await response.json();
                communities.forEach(community => {
                    this.addCommunity(community);
                });
            }
        } catch (error) {
            console.error('Error fetching community details:', error);
        }
    }

    showSearchResults(communities) {
        this.dropdown.innerHTML = '';
        
        if (communities.length === 0) {
            this.dropdown.innerHTML = '<div class="community-dropdown-item no-results">No communities found</div>';
        } else {
            communities.forEach(community => {
                if (!this.selectedCommunities.has(community.id)) {
                    const item = document.createElement('div');
                    item.className = 'community-dropdown-item';
                    item.innerHTML = `
                        <span class="community-name">${this.escapeHtml(community.display_name || community.name)}</span>
                        ${community.instance ? `<span class="community-instance">@${this.escapeHtml(community.instance)}</span>` : ''}
                        ${community.subscribers ? `<span class="community-stats">(${community.subscribers} subscribers)</span>` : ''}
                    `;
                    
                    item.addEventListener('click', () => {
                        this.addCommunity(community);
                        this.searchInput.value = '';
                        this.hideDropdown();
                    });
                    
                    this.dropdown.appendChild(item);
                }
            });
        }
        
        this.showDropdown();
    }

    addCommunity(community) {
        if (this.selectedCommunities.has(community.id)) {
            return;
        }
        
        this.selectedCommunities.set(community.id, community);
        
        // Create chip
        const chip = document.createElement('div');
        chip.className = 'community-chip';
        chip.innerHTML = `
            <span class="chip-text">${this.escapeHtml(community.display_name || community.name)}</span>
            <button type="button" class="chip-remove" data-id="${community.id}">
                ×
            </button>
        `;
        
        // Add remove handler
        chip.querySelector('.chip-remove').addEventListener('click', () => {
            this.removeCommunity(community.id);
        });
        
        // Insert chip before the search wrapper
        this.chipsContainer.insertBefore(chip, this.searchWrapper);
        this.updateInputValue();
    }

    removeCommunity(communityId) {
        this.selectedCommunities.delete(communityId);
        
        // Remove chip
        const chip = this.chipsContainer.querySelector(`[data-id="${communityId}"]`).parentElement;
        chip.remove();
        
        this.updateInputValue();
    }

    updateInputValue() {
        this.input.value = Array.from(this.selectedCommunities.keys()).join(',');
    }

    showDropdown() {
        this.dropdown.style.display = 'block';
    }

    hideDropdown() {
        this.dropdown.style.display = 'none';
    }

    escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }
}

// Initialize when DOM is ready
document.addEventListener('DOMContentLoaded', function() {
    const communityInput = document.getElementById('community-select');
    if (communityInput) {
        new ReportsCommunityMultiSelect(communityInput);
    }
});