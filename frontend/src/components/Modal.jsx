import { useEffect } from "react";

export function Modal({ title, onClose, children, width = 480 }) {
  useEffect(() => {
    function handleKeyDown(event) {
      if (event.key === "Escape") onClose();
    }
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [onClose]);

  return (
    <div className="modal-overlay" onMouseDown={onClose}>
      <div
        className="modal"
        style={{ maxWidth: width }}
        onMouseDown={(event) => event.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-label={title}
      >
        <div className="modal__header">
          <h2>{title}</h2>
          <button type="button" className="modal__close" onClick={onClose} aria-label="Close">
            &times;
          </button>
        </div>
        <div className="modal__body">{children}</div>
      </div>
    </div>
  );
}
