"use client";

import { useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { X, ZoomIn, ZoomOut, ExternalLink } from "lucide-react";
import { absoluteMediaUrl, safeHref } from "@/services/chat-dashboard.service";

interface Props {
  url: string;
  title?: string;
  driveLink?: string | null;
  onClose: () => void;
}

/** Full-screen image viewer: click or use the buttons / mouse wheel to zoom, Esc to close.
 *  Downloading happens in Google Drive ("Open in Drive"), as the owners asked. */
export default function ImageLightbox({ url, title, driveLink, onClose }: Props) {
  const [zoom, setZoom] = useState(1);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
      if (e.key === "+" || e.key === "=") setZoom((z) => Math.min(z + 0.5, 6));
      if (e.key === "-") setZoom((z) => Math.max(z - 0.5, 1));
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  // Drawn straight into <body>: the page's animated wrappers would otherwise shrink "full screen" to the content area.
  if (typeof document === "undefined") return null;
  return createPortal(
    <div
      className="fixed inset-0 z-[100] flex flex-col"
      style={{ background: "rgba(0,0,0,0.88)" }}
      onClick={onClose}
      role="dialog"
      aria-modal="true"
    >
      <div className="flex items-center justify-between gap-3 px-4 py-3 text-sm text-white" onClick={(e) => e.stopPropagation()}>
        <span className="truncate opacity-80">{title ?? "Image"}</span>
        <div className="flex items-center gap-2">
          <button className="rounded p-2 hover:bg-white/10" aria-label="Zoom out" onClick={() => setZoom((z) => Math.max(z - 0.5, 1))}>
            <ZoomOut size={18} />
          </button>
          <span className="w-10 text-center text-xs opacity-70">{Math.round(zoom * 100)}%</span>
          <button className="rounded p-2 hover:bg-white/10" aria-label="Zoom in" onClick={() => setZoom((z) => Math.min(z + 0.5, 6))}>
            <ZoomIn size={18} />
          </button>
          {safeHref(driveLink) && (
            <a
              href={safeHref(driveLink)}
              target="_blank"
              rel="noreferrer"
              className="flex items-center gap-1 rounded bg-white/10 px-3 py-1.5 text-xs hover:bg-white/20"
            >
              <ExternalLink size={14} /> Open in Drive (download)
            </a>
          )}
          <button className="rounded p-2 hover:bg-white/10" aria-label="Close" onClick={onClose}>
            <X size={20} />
          </button>
        </div>
      </div>
      <div
        className="flex-1 overflow-auto"
        onClick={(e) => e.stopPropagation()}
        onWheel={(e) => setZoom((z) => Math.min(Math.max(z + (e.deltaY < 0 ? 0.25 : -0.25), 1), 6))}
      >
        <div className="flex min-h-full min-w-full items-center justify-center p-4">
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img
            src={absoluteMediaUrl(url)}
            alt={title ?? "Image"}
            onClick={() => setZoom((z) => (z >= 3 ? 1 : z + 1))}
            style={{ width: `${zoom * 100}%`, maxWidth: zoom === 1 ? "min(100%, 80vh)" : "none", cursor: zoom >= 3 ? "zoom-out" : "zoom-in" }}
            className="select-none"
            draggable={false}
          />
        </div>
      </div>
    </div>,
    document.body,
  );
}
