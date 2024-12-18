if(!setTheme) {
    const setTheme = theme => {
        if (theme === 'auto' && window.matchMedia('(prefers-color-scheme: dark)').matches) {
          document.documentElement.setAttribute('data-bs-theme', 'dark')
        } else {
          document.documentElement.setAttribute('data-bs-theme', theme)
        }
    }
}

// fires after DOM is ready for manipulation
document.addEventListener("DOMContentLoaded", function () {
    if(navigator.getBattery) {
        navigator.getBattery().then(function(battery) {
            // Only load youtube videos in teasers if there is plenty of power available
            if (battery.charging) {
                setupYouTubeLazyLoad();
            }
        });
    }
    setupCommunityNameInput();
    setupShowMoreLinks();
    setupConfirmFirst();
    setupSubmitOnInputChange();
    setupTimeTracking();
    setupMobileNav();
    setupLightDark();
    setupKeyboardShortcuts();
    setupTopicChooser();
    setupConversationChooser();
    setupMarkdownEditorEnabler();
    setupAddPollChoice();
    setupShowElementLinks();
    setupLightboxTeaser();
    setupLightboxPostBody();
    setupPostTeaserHandler();
});

function setupPostTeaserHandler() {
    document.querySelectorAll('.post_teaser_clickable').forEach(div => {
        div.onclick = function() {
            const firstAnchor = this.parentElement.querySelector('h3 a');
            if (firstAnchor) {
                window.location.href = firstAnchor.href;
            }
        };
    });
}

function setupYouTubeLazyLoad() {
    const lazyVideos = document.querySelectorAll(".video-wrapper");

    if ("IntersectionObserver" in window) {
        let videoObserver = new IntersectionObserver((entries, observer) => {
            entries.forEach((entry) => {
                if (entry.isIntersecting) {
                    let videoWrapper = entry.target;
                    let iframe = document.createElement("iframe");
                    iframe.src = videoWrapper.getAttribute("data-src");
                    iframe.allow = "accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture; fullscreen";

                    videoWrapper.innerHTML = "";
                    videoWrapper.appendChild(iframe);

                    videoObserver.unobserve(videoWrapper);
                }
            });
        }, {
            rootMargin: "0px 0px 300px 0px" // Preload when 300px away from the viewport
        });

        lazyVideos.forEach((video) => {
            videoObserver.observe(video);
        });
    } else {
        // Fallback for older browsers
        lazyVideos.forEach((video) => {
            let iframe = document.createElement("iframe");
            iframe.src = video.getAttribute("data-src");
            iframe.allow = "accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture; fullscreen";

            video.innerHTML = "";
            video.appendChild(iframe);
        });
    }
}

// All elements with the class "showElement" will show the DOM element referenced by the data-id attribute
function setupShowElementLinks() {
    var elements = document.querySelectorAll('.showElement');
    elements.forEach(function(element) {
        element.addEventListener('click', function(event) {
            event.preventDefault();
            var dataId = this.getAttribute('data-id');
            var targetElement = document.getElementById(dataId);
            if (targetElement) {
                targetElement.style.display = 'inherit';
            }
        });
    });
}

function renderMasonry(masonry, htmlSnippets) {
      const mainPane = document.querySelector('.main_pane');
      const mainPaneWidth = mainPane.offsetWidth;
      let numColumns;

      if (mainPaneWidth < 600) {
          numColumns = 2; // 2 columns for mobile
      } else if (mainPaneWidth < 992) {
          numColumns = 3; // 3 columns for phablet
      } else if (mainPaneWidth < 1200) {
          numColumns = 4; // 4 columns for tablet or laptop
      } else {
          numColumns = 5; // 5 columns for larger screens
      }
      const columns = [];

      // Create and append column divs
      for (let i = 0; i < numColumns; i++) {
        const column = document.createElement('div');
        column.classList.add('column');
        masonry.appendChild(column);
        columns.push(column);
      }

      // Distribute HTML snippets to columns
      htmlSnippets.forEach(function(htmlSnippet, index) {
        const columnIndex = index % numColumns;
        const column = columns[columnIndex];
        const item = document.createElement('div');
        item.innerHTML = htmlSnippet;
        column.appendChild(item);
      });

      setupLightboxGallery();
}

function setupLightboxGallery() {
    // Check if there are elements with either "post_list_masonry_wide" or "post_list_masonry" class
    var galleryPosts = document.querySelectorAll('.masonry');

    // Enable lightbox on masonry images
    if (galleryPosts.length > 0) {
        baguetteBox.run('.masonry', {
            fullScreen: false,
            titleTag: true,
            preload: 5,
            captions: function(element) {
                return element.getElementsByTagName('img')[0].title;
            }
        });
    }
}


function setupLightboxTeaser() {
    function popStateListener(event) {
        baguetteBox.hide();
    }
    baguetteBox.run('.post_teaser', {
        fullScreen: false,
        noScrollbars: true,
        async: true,
        preload: 3,
        ignoreClass: 'preview_image',
        afterShow: function() {
            window.history.pushState('#lightbox', document.title, document.location+'#lightbox');
            window.addEventListener('popstate', popStateListener);

            function baguetteBoxClickImg(event) {
              if (this.style.width != "100vw" && this.offsetWidth < window.innerWidth) {
                this.style.width = "100vw";
                this.style.maxHeight = "none";
              } else {
                this.style.width = "";
                this.style.maxHeight = "";
                this.removeEventListener('click', baguetteBoxClickImg);
                baguetteBox.hide();
              }
            };
            for (const el of document.querySelectorAll('div#baguetteBox-overlay img')) {
              el.addEventListener('click', baguetteBoxClickImg);
            }
        },
        afterHide: function() {
            if (window.history.state === '#lightbox') {
              window.history.back();
              window.removeEventListener('popstate', popStateListener);
            }
        },
    });
}

function setupLightboxPostBody() {
    const images = document.querySelectorAll('.post_body img');
    images.forEach(function(img) {
        const parent = img.parentNode;
        const link = document.createElement('a');
        link.href = img.src;
        link.setAttribute('data-caption', img.alt);
        parent.replaceChild(link, img);
        link.appendChild(img);
    });

    baguetteBox.run('.post_body', {
        fullScreen: false,
        titleTag: true,
        async: true,
        preload: 3
    });
}

// fires after all resources have loaded, including stylesheets and js files
window.addEventListener("load", function () {
    setupHideButtons();
});

function setupMobileNav() {
    var navbarToggler = document.getElementById('navbar-toggler');
    var navbarSupportedContent = document.getElementById('navbarSupportedContent');
    navbarToggler.addEventListener("click", function(event) {
        toggleClass('navbarSupportedContent', 'show_menu');
        var isExpanded = navbarSupportedContent.classList.contains('show_menu');
        navbarToggler.setAttribute('aria-expanded', isExpanded ? 'true' : 'false');
        navbarSupportedContent.setAttribute('aria-expanded', isExpanded ? 'true' : 'false');
    });
    if(window.innerWidth < 992) {
        navbarToggler.setAttribute('aria-expanded', 'false');
    }
}

function setupLightDark() {
    const elem = document.getElementById('light_mode');
    elem.addEventListener("click", function(event) {
        setTheme('light')
        setStoredTheme('light')
    });
    const elem2 = document.getElementById('dark_mode');
    elem2.addEventListener("click", function(event) {
        setTheme('dark')
        setStoredTheme('dark')
    });
}

function toggleClass(elementId, className) {
  var element = document.getElementById(elementId);

  if (element.classList.contains(className)) {
    // If the element has the class, remove it
    element.classList.remove(className);
  } else {
    // If the element doesn't have the class, add it
    element.classList.add(className);
  }
}

function findOutermostParent(element, className) {
  while (element && !element.classList.contains(className)) {
    element = element.parentNode;
  }
  return element;
}

function setupAutoResize(element) {
    const elem = document.getElementById(element);
    elem.addEventListener("keyup", function(event) {
        const outerWrapper = findOutermostParent(elem, 'downarea');
        elem.style.height = 'auto'; // Reset height to auto to calculate scrollHeight accurately
        elem.style.height = (elem.scrollHeight + 2) + 'px'; // Add 2px to avoid cutting off text
        outerWrapper.style.height = (elem.scrollHeight + 61) + 'px';
    });

}


// disabled for now
function setupImageExpander() {
    // Get all elements with the class "preview_image"
    var imageLinks = document.querySelectorAll('.preview_image');

    // Loop through each element and attach a click event listener
    imageLinks.forEach(function(link) {
      link.addEventListener('click', function(event) {
        event.preventDefault(); // Prevent the default behavior of the anchor link

        // Check if the image is already visible
        var image = this.nextElementSibling; // Assumes the image is always the next sibling
        var isImageVisible = image && image.style.display !== 'none';

        // Toggle the visibility of the image
        if (isImageVisible) {
          image.remove(); // Remove the image from the DOM
        } else {
          image = document.createElement('img');
          image.src = this.href; // Set the image source to the href of the anchor link
          image.alt = 'Image'; // Set the alt attribute for accessibility
          image.className = 'preview_image_shown';

          // Add click event listener to the inserted image
          image.addEventListener('click', function() {
            // Replace location.href with the URL of the clicked image
            window.location.href = image.src;
          });

          // Insert the image after the anchor link
          this.parentNode.insertBefore(image, this.nextSibling);
        }

        // Toggle a class on the anchor to indicate whether the image is being shown or not
        this.classList.toggle('imageVisible', !isImageVisible);
      });
    });
}

function collapseReply(comment_id) {
    const reply = document.getElementById('comment_' + comment_id);
    let isHidden = false;
    if(reply) {
        const hidables = parentElement.querySelectorAll('.hidable');

        hidables.forEach(hidable => {
            hidable.style.display = isHidden ? 'block' : 'none';
        });

        const moreHidables = parentElement.parentElement.querySelectorAll('.hidable');
        moreHidables.forEach(hidable => {
            hidable.style.display = isHidden ? 'block' : 'none';
        });

        // Toggle the content of hideEl
        if (isHidden) {
            hideEl.innerHTML = "<a href='#'>[-] hide</a>";
        } else {
            hideEl.innerHTML = "<a href='#'>[+] show</a>";
        }

        isHidden = !isHidden; // Toggle the state
    }
}

// every element with the 'confirm_first' class gets a popup confirmation dialog
function setupConfirmFirst() {
    const show_first = document.querySelectorAll('.confirm_first');
    show_first.forEach(element => {
        element.addEventListener("click", function(event) {
            if (!confirm("Are you sure?")) {
              event.preventDefault(); // As the user clicked "Cancel" in the dialog, prevent the default action.
            }
        });
    });

    const go_back = document.querySelectorAll('.go_back');
    go_back.forEach(element => {
        element.addEventListener("click", function(event) {
            history.back();
            event.preventDefault();
            return false;
        });
    })

    const redirect_login = document.querySelectorAll('.redirect_login');
    redirect_login.forEach(element => {
        element.addEventListener("click", function(event) {
            location.href = '/auth/login';
            event.preventDefault();
            return false;
        });
    });
}

function setupSubmitOnInputChange() {
    const inputElements = document.querySelectorAll('.submit_on_change');

    inputElements.forEach(element => {
        element.addEventListener("change", function() {
            const form = findParentForm(element);
            if (form) {
                form.submit();
            }
        });
    });
}

// Find the parent form of an element
function findParentForm(element) {
    let currentElement = element;
    while (currentElement) {
        if (currentElement.tagName === 'FORM') {
            return currentElement;
        }
        currentElement = currentElement.parentElement;
    }
    return null;
}

function setupShowMoreLinks() {
    const comments = document.querySelectorAll('.comment');

    comments.forEach(comment => {
        const content = comment.querySelector('.limit_height');
        if (content && content.clientHeight > 400) {
            content.style.overflow = 'hidden';
            content.style.maxHeight = '400px';
            const showMoreLink = document.createElement('a');
            showMoreLink.classList.add('show-more');
            showMoreLink.classList.add('hidable');
            showMoreLink.innerHTML = '<i class="fe fe-angles-down" title="Read more"></i>';
            showMoreLink.href = '#';
            showMoreLink.addEventListener('click', function(event) {
                event.preventDefault();
                content.classList.toggle('expanded');
                if (content.classList.contains('expanded')) {
                    content.style.overflow = 'visible';
                    content.style.maxHeight = '';
                    showMoreLink.innerHTML = '<i class="fe fe-angles-up" title="Collapse"></i>';
                } else {
                    content.style.overflow = 'hidden';
                    content.style.maxHeight = '400px';
                    showMoreLink.innerHTML = '<i class="fe fe-angles-down" title="Read more"></i>';
                }
            });
            content.insertAdjacentElement('afterend', showMoreLink);
        }
    });
}

function setupCommunityNameInput() {
   var communityNameInput = document.getElementById('community_name');

   if (communityNameInput) {
       communityNameInput.addEventListener('keyup', function() {
          var urlInput = document.getElementById('url');
          urlInput.value = titleToURL(communityNameInput.value);
       });
   }
}


function setupHideButtons() {
    const hideEls2 = document.querySelectorAll('.hide_button a');
    hideEls2.forEach(hideEl => {
        let isHidden = false;

        hideEl.addEventListener('click', event => {
            event.preventDefault();
            const parentElement = hideEl.parentElement.parentElement;
            const hidables = parentElement.parentElement.querySelectorAll('.hidable');

            hidables.forEach(hidable => {
                hidable.style.display = 'none';
            });

            const unhide = parentElement.parentElement.querySelectorAll('.unhide');
            unhide[0].style.display = 'inline-block';
        });
    });

    const showEls = document.querySelectorAll('a.unhide');
    showEls.forEach(showEl => {
        showEl.addEventListener('click', event => {
            event.preventDefault();
            showEl.style.display = 'none';
            const hidables = showEl.parentElement.parentElement.parentElement.querySelectorAll('.hidable');
            hidables.forEach(hidable => {
                hidable.style.display = '';
            });
        });
    });

    if(typeof toBeHidden !== "undefined" && toBeHidden) {
        toBeHidden.forEach((arrayElement) => {
          // Build the ID of the outer div
          const divId = "comment_" + arrayElement;

          // Access the outer div by its ID
          const commentDiv = document.getElementById(divId);

          if (commentDiv) {
            // Access the inner div with class "hide_button" inside the outer div
            const hideButton = commentDiv.querySelectorAll(".hide_button a");

            if (hideButton) {
              // Programmatically trigger a click event on the "hide_button" anchor
              hideButton[0].click();
            } else {
              console.log(`"hide_button" not found in ${divId}`);
            }
          } else {
            console.log(`Div with ID ${divId} not found`);
          }
        });
    }
}

function titleToURL(title) {
  // Convert the title to lowercase and replace spaces with hyphens
  return title.toLowerCase().replace(/\s+/g, '_');
}

var timeTrackingInterval;
var currentlyVisible = true;

function setupTimeTracking() {
    // Check for Page Visibility API support
    if (document.visibilityState) {
        const lastUpdate = new Date(localStorage.getItem('lastUpdate')) || new Date();

       // Initialize variables to track time
       let timeSpent = parseInt(localStorage.getItem('timeSpent')) || 0;

       displayTimeTracked();

       timeTrackingInterval = setInterval(() => {
          timeSpent += 2;
          localStorage.setItem('timeSpent', timeSpent);
          // Display timeSpent
          displayTimeTracked();
       }, 2000)


       // Event listener for visibility changes
       document.addEventListener("visibilitychange", function() {
          const currentDate = new Date();

          if (currentDate.getMonth() !== lastUpdate.getMonth() || currentDate.getFullYear() !== lastUpdate.getFullYear()) {
            // Reset counter for a new month
            timeSpent = 0;
            localStorage.setItem('timeSpent', timeSpent);
            localStorage.setItem('lastUpdate', currentDate.toString());
            displayTimeTracked();
          }

          if (document.visibilityState === "visible") {
              console.log('visible')
              currentlyVisible = true
              timeTrackingInterval = setInterval(() => {
                  timeSpent += 2;
                  localStorage.setItem('timeSpent', timeSpent);
                  displayTimeTracked();
              }, 2000)
          } else {
              currentlyVisible = false;
              if(timeTrackingInterval) {
                 clearInterval(timeTrackingInterval);
              }
          }
       });
    }
}

var currentPost;                        // keep track of which is the current post. Set by mouse movements (see const votableElements) and by J and K key presses
var showCurrentPost = false;    // when true, the currently selected post will be visibly different from the others. Set to true by J and K key presses

function setupKeyboardShortcuts() {
    document.addEventListener('keydown', function(event) {
        if (document.activeElement.tagName !== 'INPUT' && document.activeElement.tagName !== 'TEXTAREA') {
            if(document.activeElement.classList.contains('skip-link')) {
                return;
            }
            var didSomething = false;
            if(event.shiftKey && event.key === '?') {
                location.href = '/keyboard_shortcuts';
                didSomething = true;
            } else if (event.key === 'a') {
                if(currentPost) {
                    currentPost.querySelector('.upvote_button').click();
                    didSomething = true;
                }
            } else if (event.key === 'z') {
                if(currentPost) {
                    currentPost.querySelector('.downvote_button').click();
                    didSomething = true;
                }
            } else if (event.key === 'x') {
                if(currentPost) {
                    currentPost.querySelector('.preview_image').click();
                    didSomething = true;
                }
            } else if (event.key === 'l') {
                if (currentPost) {
                    currentPost.querySelector('.post_link').click();
                    didSomething = true;
                }
            } else if (event.key === 'Enter') {
                if(currentPost && document.activeElement.tagName !== 'a') {
                    currentPost.querySelector('.post_teaser_title_a').click();
                    didSomething = true;
                }
            } else if (event.key === 'j') {
                showCurrentPost = true;
                if(currentPost) {
                    if(currentPost.nextElementSibling) {
                        var elementToRemoveClass = document.querySelector('.post_teaser.current_post');
                        if(elementToRemoveClass)
                            elementToRemoveClass.classList.remove('current_post');
                        currentPost = currentPost.nextElementSibling;
                        currentPost.classList.add('current_post');
                    }
                    didSomething = true;
                }
                else {
                    currentPost = document.querySelector('.post_teaser');
                    currentPost.classList.add('current_post');
                }
                // Check if the current post is out of the viewport
                var rect = currentPost.getBoundingClientRect();
                if (rect.bottom > window.innerHeight || rect.top < 0) {
                    currentPost.scrollIntoView({ behavior: 'smooth', block: 'start' });
                }
            } else if (event.key === 'k') {
                showCurrentPost = true;
                if(currentPost) {
                    if(currentPost.previousElementSibling) {
                        var elementToRemoveClass = document.querySelector('.post_teaser.current_post');
                        if(elementToRemoveClass)
                            elementToRemoveClass.classList.remove('current_post');
                        currentPost = currentPost.previousElementSibling;
                        currentPost.classList.add('current_post');
                    }
                    didSomething = true;
                }
                else {
                    currentPost = document.querySelector('.post_teaser');
                    currentPost.classList.add('current_post');
                }
                // Check if the current post is out of the viewport
                var rect = currentPost.getBoundingClientRect();
                if (rect.bottom > window.innerHeight || rect.top < 0) {
                    currentPost.scrollIntoView({ behavior: 'smooth', block: 'end' });
                }
            }
            if(didSomething) {
                event.preventDefault();
            }
        }

        // While typing a post or reply, Ctrl + Enter submits the form
        if(document.activeElement.tagName === 'TEXTAREA') {
            if (event.ctrlKey && event.key === 'Enter') {
                var form = document.activeElement.closest('form');
                if (form) {
                    form.submit.click();
                }
            }
        }
    });

    const votableElements = document.querySelectorAll('.post_teaser, .post_type_image, .post_type_normal');
    votableElements.forEach(votable => {
        votable.addEventListener('mouseover', event => {
            currentPost = event.currentTarget;
            if(showCurrentPost) {
                var elementToRemoveClass = document.querySelector('.post_teaser.current_post');
                elementToRemoveClass.classList.remove('current_post');
                currentPost.classList.add('current_post');
            }
        });
        votable.addEventListener('mouseout', event => {
            //currentPost = null;
            if(showCurrentPost) {
                //var elementToRemoveClass = document.querySelector('.post_teaser.current_post');
                //elementToRemoveClass.classList.remove('current_post');
            }
        });
    });
}

function setupTopicChooser() {
    // at /topic/news/submit, clicking on an anchor element needs to save the clicked community id to a hidden field and then submit the form
    var chooseTopicLinks = document.querySelectorAll('a.choose_topic_for_post');
    chooseTopicLinks.forEach(function(link) {
        link.addEventListener('click', function(event) {
            event.preventDefault();
            var communityIdInput = document.getElementById('community_id');
            var communityForm = document.getElementById('choose_community');

            // Set the value of the hidden input field
            if (communityIdInput) {
                communityIdInput.value = this.getAttribute('data-id');
            }
            if (communityForm) {
                communityForm.submit();
            }
        });
    });
}

function setupConversationChooser() {
    const changeSender = document.getElementById('changeSender');
    if(changeSender) {
        changeSender.addEventListener('change', function() {
            const user_id = changeSender.options[changeSender.selectedIndex].value;
            location.href = '/chat/' + user_id;
        });
    }
}

function formatTime(seconds) {
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);

  let result = '';

  if (hours > 0) {
    result += `${hours} ${hours === 1 ? 'hour' : 'hours'}`;
  }

  if (minutes > 0) {
    if (result !== '') {
      result += ' ';
    }
    result += `${minutes} ${minutes === 1 ? 'minute' : 'minutes'}`;
  }

  if (result === '') {
    result = 'Less than a minute';
  }

  return result;
}

function displayTimeTracked() {
    const timeSpentElement = document.getElementById('timeSpent');
    let timeSpent = parseInt(localStorage.getItem('timeSpent')) || 0;
    if(timeSpentElement && timeSpent) {
        timeSpentElement.textContent = formatTime(timeSpent)
    }
}

function setupMarkdownEditorEnabler() {
    const markdownEnablerLinks = document.querySelectorAll('.markdown_editor_enabler');
    markdownEnablerLinks.forEach(function(link) {
        link.addEventListener('click', function(event) {
            event.preventDefault();
            const dataId = link.dataset.id;
            if(dataId) {
                var downarea = new DownArea({
                    elem: document.querySelector('#' + dataId),
                    resize: DownArea.RESIZE_VERTICAL,
                    hide: ['heading', 'bold-italic'],
                    value: document.getElementById(dataId).value
                });
                setupAutoResize(dataId);
                link.style.display = 'none';
            }
        });
    });
}

function setupAddPollChoice() {
    const addChoiceButton = document.getElementById('addPollChoice');
    const pollChoicesFieldset = document.getElementById('pollChoicesFieldset');
    if(pollChoicesFieldset == null) {
        return;
    }
    const formGroups = pollChoicesFieldset.getElementsByClassName('form-group');

    if(addChoiceButton && addChoiceButton) {
        addChoiceButton.addEventListener('click', function(event) {
            // Loop through the form groups and show the first hidden one
            for (let i = 0; i < formGroups.length; i++) {
                if (formGroups[i].style.display === 'none') {
                    formGroups[i].style.display = 'block';
                    break; // Stop once we've shown the next hidden form group
                }
            }
        });
    }
}

function getCookie(name) {
    var cookies = document.cookie.split(';');

    for (var i = 0; i < cookies.length; i++) {
        var cookie = cookies[i].trim();

        if (cookie.indexOf(name + '=') === 0) {
            return cookie.substring(name.length + 1);
        }
    }

    return null;
}

/* register a service worker */
if ('serviceWorker' in navigator) {
  window.addEventListener('load', function() {
    navigator.serviceWorker.register('/static/service_worker.js', {scope: '/static/'}).then(function(registration) {
      // Registration was successful
      // console.log('ServiceWorker2 registration successful with scope: ', registration.scope);
    }, function(err) {
      // registration failed :(
      console.log('ServiceWorker registration failed: ', err);
    });
  });
}
