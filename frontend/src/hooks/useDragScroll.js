import { useCallback, useRef } from 'react';

/**
 * Seitwärts wischen ("Drag-Scroll") für einzeilige Leisten.
 *
 * Touch scrollt nativ; mit der Maus kann überall in der Leiste gegriffen und
 * gewischt werden – auch auf Buttons. Ein Klick wird nur dann unterdrückt,
 * wenn wirklich gezogen wurde. Optional übersetzt das Mausrad vertikale
 * Bewegung in horizontales Scrollen.
 *
 * Verwendung:
 *   const drag = useDragScroll();
 *   <div className="…" {...drag.props}>…</div>
 */
export default function useDragScroll({ wheel = true, threshold = 4 } = {}) {
  const ref = useRef(null);
  const moved = useRef(false);

  const onPointerDown = useCallback((e) => {
    const el = ref.current;
    if (!el || e.pointerType === 'touch') return;
    if (e.target.closest && e.target.closest('input, select, textarea')) return;
    if (el.scrollWidth <= el.clientWidth) return;
    const startX = e.clientX;
    const startScroll = el.scrollLeft;
    moved.current = false;
    const move = (ev) => {
      const dx = ev.clientX - startX;
      if (Math.abs(dx) > threshold) {
        moved.current = true;
        el.classList.add('dragging');
      }
      el.scrollLeft = startScroll - dx;
    };
    const up = () => {
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', up);
      el.classList.remove('dragging');
      // Erst nach dem Click-Event zurücksetzen, damit der Klick unterdrückt wird
      setTimeout(() => { moved.current = false; }, 0);
    };
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', up);
  }, [threshold]);

  const onWheelHandler = useCallback((e) => {
    const el = ref.current;
    if (!el || el.scrollWidth <= el.clientWidth) return;
    if (Math.abs(e.deltaY) > Math.abs(e.deltaX)) {
      el.scrollLeft += e.deltaY;
      e.preventDefault();
    }
  }, []);

  const onClickCapture = useCallback((e) => {
    if (moved.current) { e.preventDefault(); e.stopPropagation(); }
  }, []);

  return {
    ref,
    moved,
    props: {
      ref,
      onPointerDown,
      onClickCapture,
      ...(wheel ? { onWheel: onWheelHandler } : {}),
    },
  };
}
