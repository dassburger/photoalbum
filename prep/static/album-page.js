/* Album page progressive enhancement (Task 14):
 *  - hover-activated left/right scroll buttons (markup + visibility CSS
 *    from Task 8; this file only wires up click handling plus a boundary
 *    class toggle — it never sets an inline style for visibility).
 *  - native scroll-snap + overflow-x: auto already gives drag/swipe for
 *    free, so no custom swipe handling is added here.
 *  - shrink-to-fit note sizing: binary-search a --note-font-size custom
 *    property per .note-slot until its text fits the slot's fixed grid
 *    cell. Per the project's "custom-properties-only style attribute"
 *    rule, this is always set via element.style.setProperty() on a
 *    `--custom-property`, never a literal CSS property.
 *
 * No build step / no framework — plain DOM APIs only, loaded deferred
 * (see the <script defer> tag in album_page.html) so it never blocks
 * rendering and runs after the DOM is parsed.
 */
(function () {
  "use strict";

  // -- hover-activated scroll buttons ------------------------------------

  function initScrollButtons() {
    var wraps = document.querySelectorAll(".album-grid-wrap");

    wraps.forEach(function (wrap) {
      var grid = wrap.querySelector(".album-grid");
      var prevButton = wrap.querySelector(".scroll-button--prev");
      var nextButton = wrap.querySelector(".scroll-button--next");

      if (!grid || !prevButton || !nextButton) {
        return;
      }

      function scrollByAmount(direction) {
        var amount = Math.max(grid.clientWidth * 0.8, 200);
        grid.scrollBy({ left: direction * amount, behavior: "smooth" });
      }

      prevButton.addEventListener("click", function () {
        scrollByAmount(-1);
      });
      nextButton.addEventListener("click", function () {
        scrollByAmount(1);
      });

      // The buttons' show/hide-on-hover is pure CSS (.album-grid-wrap:hover
      // / :focus-within in album-page.css). The only thing JS adds is a
      // boundary class so a button that would do nothing (already at the
      // start/end of the scrollable area) hides itself too — toggled as a
      // class, never an inline style, matching the project-wide rule.
      function updateBoundaryClasses() {
        var atStart = grid.scrollLeft <= 1;
        var atEnd = grid.scrollLeft + grid.clientWidth >= grid.scrollWidth - 1;
        wrap.classList.toggle("is-at-start", atStart);
        wrap.classList.toggle("is-at-end", atEnd);
      }

      var scrollUpdatePending = false;
      grid.addEventListener("scroll", function () {
        if (scrollUpdatePending) {
          return;
        }
        scrollUpdatePending = true;
        window.requestAnimationFrame(function () {
          updateBoundaryClasses();
          scrollUpdatePending = false;
        });
      });

      updateBoundaryClasses();
    });
  }

  // -- note-text shrink-to-fit -------------------------------------------

  var NOTE_FONT_MIN_PX = 10;
  var NOTE_FONT_MAX_PX = 48;
  // Matches album-page.css's narrow-screen collapse breakpoint, where
  // notes render as normal readable paragraphs (font-size reset with
  // !important) instead of a fitted-to-slot size — fitting is a
  // wide-screen-only nicety, so skip the work entirely below that width.
  var NARROW_MEDIA_QUERY = "(max-width: 640px)";

  function fitNoteText(slot) {
    var text = slot.querySelector(".note-slot__text");
    if (!text) {
      return;
    }

    var lo = NOTE_FONT_MIN_PX;
    var hi = NOTE_FONT_MAX_PX;
    var best = NOTE_FONT_MIN_PX;

    while (lo <= hi) {
      var mid = Math.floor((lo + hi) / 2);
      slot.style.setProperty("--note-font-size", mid + "px");
      var fits =
        text.scrollHeight <= slot.clientHeight &&
        text.scrollWidth <= slot.clientWidth;
      if (fits) {
        best = mid;
        lo = mid + 1;
      } else {
        hi = mid - 1;
      }
    }

    slot.style.setProperty("--note-font-size", best + "px");
  }

  function fitAllNotes() {
    if (window.matchMedia && window.matchMedia(NARROW_MEDIA_QUERY).matches) {
      return;
    }
    document.querySelectorAll(".note-slot").forEach(function (slot) {
      fitNoteText(slot);
    });
  }

  function debounce(fn, waitMs) {
    var timer = null;
    return function () {
      window.clearTimeout(timer);
      timer = window.setTimeout(fn, waitMs);
    };
  }

  // -- init ----------------------------------------------------------------

  function init() {
    initScrollButtons();
    fitAllNotes();
    // Re-fit on resize (debounced) so rotating a tablet, or crossing the
    // narrow-screen breakpoint, doesn't leave a stale fitted size.
    window.addEventListener("resize", debounce(fitAllNotes, 150));
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
