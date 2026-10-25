/* Picture view progressive enhancement (Task 15):
 *  - left/right-arrow-key and swipe-left/right navigation that simply
 *    *follows* the page's own real prev/next <a href> links (rendered by
 *    prep/render.py's render_picture_pages) rather than recomputing or
 *    duplicating any link-resolution logic — if a link isn't present
 *    (first/last item in the album) there is nothing to follow and the
 *    corresponding gesture/key is a no-op, matching the no-JS fallback.
 *  - Space is an additional "next" shortcut, but *only* on photo pages:
 *    on a video page Space is left alone so the native <video> element's
 *    own play/pause shortcut keeps working.
 *  - swipe is only recognized when the gesture starts outside the native
 *    <video> element, so it never fights with scrubbing/seeking on video
 *    pages.
 *  - hover-activated visibility of the left/right nav buttons is pure CSS
 *    (picture-view.css, same hidden-by-default/reveal-on-hover pattern as
 *    Task 14's album-grid scroll buttons) — there is no boundary case to
 *    track in JS here (unlike the continuously-scrollable album grid),
 *    because render_picture_pages() already omits the <a> entirely when
 *    there is no prev/next item, so a no-op button never exists in the
 *    first place.
 *
 * No build step / no framework — plain DOM APIs only, loaded deferred (see
 * the <script defer> tag in picture_view.html) so it never blocks
 * rendering and runs after the DOM is parsed.
 */
(function () {
  "use strict";

  var SWIPE_THRESHOLD_PX = 50;

  function followLink(link) {
    if (!link) {
      return false;
    }
    link.click();
    return true;
  }

  function initKeyboardNav() {
    var isVideoPage = !!document.querySelector("video.picture-view__media");

    document.addEventListener("keydown", function (event) {
      if (event.defaultPrevented || event.ctrlKey || event.metaKey || event.altKey) {
        return;
      }

      var prevLink = document.querySelector(".picture-nav-button--prev");
      var nextLink = document.querySelector(".picture-nav-button--next");

      if (event.key === "ArrowLeft") {
        if (followLink(prevLink)) {
          event.preventDefault();
        }
      } else if (event.key === "ArrowRight") {
        if (followLink(nextLink)) {
          event.preventDefault();
        }
      } else if (event.key === " " || event.key === "Spacebar") {
        // Space-as-next only applies to photo pages; on a video page this
        // branch is a deliberate no-op so the native <video> element's own
        // play/pause handling for Space is left completely alone.
        if (!isVideoPage && followLink(nextLink)) {
          event.preventDefault();
        }
      }
    });
  }

  function initSwipeNav() {
    var container = document.querySelector(".picture-view");
    if (!container) {
      return;
    }

    var tracking = false;
    var startX = 0;
    var startY = 0;

    container.addEventListener(
      "touchstart",
      function (event) {
        // Never start tracking a swipe that begins on the native <video>
        // element (its own controls need unimpeded touch/scrub gestures).
        if (event.target.closest("video") || event.touches.length !== 1) {
          tracking = false;
          return;
        }
        tracking = true;
        startX = event.touches[0].clientX;
        startY = event.touches[0].clientY;
      },
      { passive: true }
    );

    container.addEventListener(
      "touchend",
      function (event) {
        if (!tracking) {
          return;
        }
        tracking = false;

        var touch = event.changedTouches[0];
        var deltaX = touch.clientX - startX;
        var deltaY = touch.clientY - startY;

        if (Math.abs(deltaX) < SWIPE_THRESHOLD_PX || Math.abs(deltaX) < Math.abs(deltaY)) {
          return;
        }

        var link = document.querySelector(
          deltaX < 0 ? ".picture-nav-button--next" : ".picture-nav-button--prev"
        );
        followLink(link);
      },
      { passive: true }
    );

    container.addEventListener("touchcancel", function () {
      tracking = false;
    });
  }

  function init() {
    initKeyboardNav();
    initSwipeNav();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
