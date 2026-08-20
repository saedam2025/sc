(() => {
  "use strict";

  const page = document.getElementById("manualViewPage");
  if (!page) return;

  const toc = document.getElementById("manualToc");
  const tocLinks = [...toc.querySelectorAll("a[data-target]")];
  const rail = document.getElementById("manualRail");
  const railLinks = rail ? [...rail.querySelectorAll("a[data-target]")] : [];
  const sections = [...document.querySelectorAll(".manual-block")];

  function activate(id){
    tocLinks.forEach(a => a.classList.toggle("active", a.dataset.target === id));
    railLinks.forEach(a => a.classList.toggle("active", a.dataset.target === id));
  }

  const observer = new IntersectionObserver(entries => {
    const active = entries
      .filter(e => e.isIntersecting)
      .sort((a,b) => b.intersectionRatio - a.intersectionRatio)[0];
    if (active) activate(active.target.id);
  }, {rootMargin:"-32% 0px -58% 0px", threshold:[0,.05,.25,.5]});

  sections.forEach(section => observer.observe(section));
  if (sections[0]) activate(sections[0].id);

  tocLinks.forEach(a => {
    a.addEventListener("click", () => toc.classList.remove("open"));
  });

  document.getElementById("manualMobileTocBtn")?.addEventListener("click", () => {
    toc.classList.add("open");
  });

  document.getElementById("manualTocClose")?.addEventListener("click", () => {
    toc.classList.remove("open");
  });

  document.addEventListener("keydown", e => {
    if (e.key === "Escape") toc.classList.remove("open");
  });

  // Image lightbox
  const lightbox = document.getElementById("manualLightbox");
  const lightboxImg = lightbox?.querySelector("img");
  const lightboxClose = lightbox?.querySelector("button");

  document.querySelectorAll(".manual-content img").forEach(img => {
    img.addEventListener("click", () => {
      if (!lightbox || !lightboxImg) return;
      lightboxImg.src = img.src;
      lightboxImg.alt = img.alt || "";
      lightbox.hidden = false;
      document.documentElement.style.overflow = "hidden";
    });
  });

  function closeLightbox(){
    if (!lightbox) return;
    lightbox.hidden = true;
    document.documentElement.style.overflow = "";
  }

  lightboxClose?.addEventListener("click", closeLightbox);
  lightbox?.addEventListener("click", e => {
    if (e.target === lightbox) closeLightbox();
  });
  document.addEventListener("keydown", e => {
    if (e.key === "Escape" && lightbox && !lightbox.hidden) closeLightbox();
  });
})();
