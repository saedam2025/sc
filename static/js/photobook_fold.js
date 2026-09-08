/* Diagonal cylindrical paper fold. Coordinates run outward from the spine.
   The two spine endpoints constrain the grabbed edge, so the crease never
   crosses the binding. No image pixels or application state are read here. */
(function (root) {
  'use strict';
  const clamp = (value, min, max) => Math.max(min, Math.min(max, value));

  function geometry(width, height, grabY, requested) {
    const origin = { x: width, y: clamp(grabY, 0, height) };
    const topRadius = Math.hypot(width, origin.y);
    const bottomRadius = Math.hypot(width, height - origin.y);
    const y = clamp(requested.y, height - bottomRadius, topRadius);
    const maxX = Math.sqrt(Math.max(0, Math.min(
      topRadius ** 2 - y ** 2, bottomRadius ** 2 - (height - y) ** 2
    )));
    const point = { x: clamp(requested.x, -maxX, maxX), y };
    const dx = origin.x - point.x, dy = origin.y - point.y;
    const distance = Math.hypot(dx, dy);
    if (distance < 0.001) return { flat: true, point, origin };
    const nx = dx / distance, ny = dy / distance;
    const middle = nx * (origin.x + point.x) / 2 + ny * (origin.y + point.y) / 2;
    const spineLimit = Math.max(0, ny * height);
    // A half cylinder consumes pi*r of paper. Reserve that length before the
    // reflection plane, and shrink the radius as the fold reaches the spine.
    const radius = Math.max(0, Math.min(width * 0.055, distance * 0.12,
      (middle - spineLimit) * 2 / Math.PI));
    const crease = middle - Math.PI * radius / 2;
    return { flat: false, point, origin, nx, ny, radius, crease, distance };
  }

  function position(fold, point) {
    if (fold.flat) return { ...point, z: 0 };
    const { nx, ny, crease, radius } = fold;
    const s = nx * point.x + ny * point.y - crease;
    if (s <= 0) return { ...point, z: 0 };
    const arc = Math.PI * radius;
    const curved = radius > 0.0001 && s < arc;
    const along = curved ? radius * Math.sin(s / radius) : arc - s;
    const z = curved ? radius * (1 - Math.cos(s / radius)) : 2 * radius;
    return { x: point.x + nx * (along - s), y: point.y + ny * (along - s), z };
  }

  function clip(polygon, nx, ny, limit, less = true) {
    const result = [];
    for (let i = 0; i < polygon.length; i++) {
      const a = polygon[i], b = polygon[(i + 1) % polygon.length];
      const da = (nx * a.x + ny * a.y - limit) * (less ? 1 : -1);
      const db = (nx * b.x + ny * b.y - limit) * (less ? 1 : -1);
      if (da <= 0) result.push(a);
      if ((da < 0 && db > 0) || (da > 0 && db < 0)) {
        const t = da / (da - db);
        result.push({ x: a.x + (b.x - a.x) * t, y: a.y + (b.y - a.y) * t });
      }
    }
    return result;
  }

  function strips(width, height, fold, count = 32) {
    const rectangle = [{ x: 0, y: 0 }, { x: width, y: 0 },
      { x: width, y: height }, { x: 0, y: height }];
    if (fold.flat) return [{ polygon: rectangle, matrix: [1, 0, 0, 1, 0, 0], back: false, shade: 0 }];
    const { nx, ny, crease, radius } = fold;
    const pieces = [];
    function add(low, high, slope, intercept, back, shade) {
      let polygon = rectangle;
      if (Number.isFinite(low)) polygon = clip(polygon, nx, ny, crease + low, false);
      if (Number.isFinite(high)) polygon = clip(polygon, nx, ny, crease + high);
      if (polygon.length < 3) return;
      const delta = slope - 1, shift = intercept - delta * crease;
      pieces.push({ polygon, back, shade, matrix: [
        1 + delta * nx * nx, delta * nx * ny, delta * nx * ny,
        1 + delta * ny * ny, nx * shift, ny * shift
      ] });
    }
    add(-Infinity, 0, 1, 0, false, 0);
    const arc = Math.PI * radius;
    if (radius > 0.0001) {
      // Even count puts the front/back boundary exactly at the silhouette.
      count += count % 2;
      for (let i = 0; i < count; i++) {
        const a = arc * i / count, b = arc * (i + 1) / count;
        const fa = radius * Math.sin(a / radius), fb = radius * Math.sin(b / radius);
        const slope = (fb - fa) / (b - a);
        const angle = Math.PI * (i + 0.5) / count;
        add(a, b, slope, fa - slope * a, i >= count / 2,
          0.36 * Math.sin(angle) ** 2 + (i >= count / 2 ? 0.025 : 0));
      }
    }
    add(arc, Infinity, -1, arc, true, 0.025);
    return pieces;
  }
  root.PhotobookFold = { geometry, position, strips };
})(typeof module === 'object' ? module.exports : window);
