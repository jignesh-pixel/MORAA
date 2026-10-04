"use client";

import { useEffect, useRef } from "react";
import { cn } from "@/lib/utils";
import { useUIStore } from "@/stores/ui-store";
import { gsap } from "gsap";
import {
  LayoutDashboard,
  PlusCircle,
  PenLine,
  Settings,
  User,
  Gem,
  MessageCircle,
} from "lucide-react";

// History & Reports are temporarily hidden from the sidebar (their pages
// remain registered in app/page.tsx for easy restoration).
const navItems = [
  { id: "dashboard", label: "Dashboard", icon: LayoutDashboard },
  { id: "chats", label: "WhatsApp Chats", icon: MessageCircle },
  { id: "new-analysis", label: "New Analysis", icon: PlusCircle },
  { id: "prompt-editor", label: "Prompt Editor", icon: PenLine },
  { id: "settings", label: "Settings", icon: Settings },
  { id: "profile", label: "Profile", icon: User },
];

export default function Sidebar() {
  const {
    currentPage,
    setCurrentPage,
    sidebarCollapsed,
    toggleSidebar,
    setSidebarCollapsed,
  } = useUIStore();
  const sidebarRef = useRef<HTMLDivElement>(null);
  const logoRef = useRef<HTMLDivElement>(null);
  const labelsRef = useRef<(HTMLSpanElement | null)[]>([]);
  const brandRef = useRef<HTMLDivElement>(null);
  const isFirstRender = useRef(true);

  // GSAP animation on collapse/expand
  useEffect(() => {
    if (isFirstRender.current) {
      isFirstRender.current = false;
      // Set initial state instantly
      gsap.set(sidebarRef.current, {
        width: sidebarCollapsed ? 68 : 240,
      });
      gsap.set(labelsRef.current.filter(Boolean), {
        opacity: sidebarCollapsed ? 0 : 1,
        display: sidebarCollapsed ? "none" : "block",
      });
      gsap.set(brandRef.current, {
        opacity: sidebarCollapsed ? 0 : 1,
        display: sidebarCollapsed ? "none" : "block",
      });
      return;
    }

    const ctx = gsap.context(() => {
      const tl = gsap.timeline({
        defaults: { ease: "power3.inOut", duration: 0.35 },
      });

      if (sidebarCollapsed) {
        // Collapse
        tl.to(sidebarRef.current, { width: 68 })
          .to(
            labelsRef.current.filter(Boolean),
            { opacity: 0, duration: 0.2, onComplete: () => {
              labelsRef.current.forEach((el) => {
                if (el) el.style.display = "none";
              });
            }},
            "-=0.15"
          )
          .to(brandRef.current, { opacity: 0, duration: 0.15, onComplete: () => {
            if (brandRef.current) brandRef.current.style.display = "none";
          }}, "-=0.2");
      } else {
        // Expand
        // Show labels and brand first (hidden), then animate
        labelsRef.current.forEach((el) => {
          if (el) {
            el.style.display = "block";
            el.style.opacity = "0";
          }
        });
        if (brandRef.current) {
          brandRef.current.style.display = "block";
          brandRef.current.style.opacity = "0";
        }

        tl.to(sidebarRef.current, { width: 240 })
          .to(labelsRef.current.filter(Boolean), { opacity: 1, duration: 0.2 }, "-=0.1")
          .to(brandRef.current, { opacity: 1, duration: 0.2 }, "-=0.15");
      }
    }, sidebarRef);

    return () => ctx.revert();
  }, [sidebarCollapsed]);

  // Auto-collapse on mobile
  useEffect(() => {
    const checkWidth = () => {
      if (window.innerWidth < 1024) {
        setSidebarCollapsed(true);
      }
    };
    checkWidth();
    window.addEventListener("resize", checkWidth);
    return () => window.removeEventListener("resize", checkWidth);
  }, [setSidebarCollapsed]);

  const handleLogoClick = () => {
    toggleSidebar();
  };

  const handleNavClick = (id: string) => {
    setCurrentPage(id);
    // On mobile, collapse after navigation
    if (window.innerWidth < 1024) {
      setSidebarCollapsed(true);
    }
  };

  return (
    <>
      {/* Mobile overlay */}
      {!sidebarCollapsed && (
        <div
          className="fixed inset-0 z-30 bg-black/50 backdrop-blur-sm lg:hidden"
          onClick={() => setSidebarCollapsed(true)}
        />
      )}

      <aside
        ref={sidebarRef}
        className={cn(
          "fixed left-0 top-0 z-40 flex h-screen flex-col border-r backdrop-blur-2xl overflow-hidden",
          "shadow-[4px_0_32px_rgba(0,0,0,0.4)]"
        )}
        style={{
          width: 240,
          backgroundColor: "var(--theme-surface)",
          borderColor: "var(--theme-border)",
        }}
      >
        {/* Top highlight */}
        <div className="absolute top-0 left-0 right-0 h-[1px] bg-gradient-to-r from-transparent via-white/[0.08] to-transparent pointer-events-none z-10" />

        {/* Logo - Click to toggle */}
        <div
          ref={logoRef}
          onClick={handleLogoClick}
          className="flex items-center h-[64px] px-4 cursor-pointer select-none group shrink-0"
          role="button"
          tabIndex={0}
          onKeyDown={(e) => {
            if (e.key === "Enter" || e.key === " ") {
              e.preventDefault();
              handleLogoClick();
            }
          }}
          aria-label={sidebarCollapsed ? "Expand sidebar" : "Collapse sidebar"}
        >
          <div className="absolute bottom-0 left-3 right-3 h-[1px] bg-gradient-to-r from-transparent via-white/[0.05] to-transparent pointer-events-none" />
          <div className="flex items-center justify-center w-10 h-10 rounded-2xl bg-gradient-to-br from-[#3B82F6] to-[#2563EB] shadow-lg shadow-blue-500/20 neumorphic transition-all duration-300 group-hover:shadow-[0_0_24px_rgba(59,130,246,0.35)] shrink-0">
            <Gem size={20} className="text-white transition-all duration-300 group-hover:scale-110 group-hover:rotate-6" />
          </div>
          <div
            ref={brandRef}
            className="ml-3 flex items-center overflow-hidden"
          >
            <span className="text-base font-bold tracking-tight whitespace-nowrap" style={{ color: "var(--theme-text)" }}>
              MORAA
            </span>
            <span className="text-base font-normal ml-1 tracking-tight whitespace-nowrap" style={{ color: "var(--theme-text-secondary)" }}>
              GemVision
            </span>
          </div>
        </div>

        {/* Navigation */}
        <nav className="flex-1 flex flex-col gap-1 py-6 px-3 overflow-y-auto overflow-x-hidden">
          {navItems.map((item, idx) => {
            const Icon = item.icon;
            const isActive = currentPage === item.id;
            return (
              <button
                key={item.id}
                onClick={() => handleNavClick(item.id)}
                className={cn(
                  "flex items-center gap-3 w-full py-2.5 px-3 rounded-2xl transition-all duration-300 group relative overflow-hidden shrink-0",
                  isActive
                    ? "text-[#3B82F6]"
                    : "hover:text-white/60"
                )}
                style={{ color: isActive ? "#3B82F6" : "var(--theme-text-secondary)" }}
                title={item.label}
                aria-current={isActive ? "page" : undefined}
              >
                {/* Ambient light behind active item */}
                {isActive && (
                  <div className="absolute -inset-4 bg-[#3B82F6] opacity-[0.12] blur-2xl rounded-full pointer-events-none transition-all duration-500" />
                )}

                {/* Premium active indicator */}
                {isActive && (
                  <span className="absolute left-0 top-1/2 -translate-y-1/2 w-[3px] h-10 rounded-r-full bg-[#3B82F6] shadow-[0_0_16px_rgba(59,130,246,0.6)] animate-breathe-glow" />
                )}

                {/* Active background */}
                {isActive && (
                  <span className="absolute inset-0 rounded-2xl bg-gradient-to-b from-[#3B82F6]/12 to-[#3B82F6]/4 border border-[#3B82F6]/12" />
                )}

                {/* Inactive hover */}
                {!isActive && (
                  <span className="absolute inset-0 rounded-2xl opacity-0 group-hover:opacity-100 bg-white/[0.03] transition-all duration-300" />
                )}

                <Icon
                  size={20}
                  className={cn(
                    "transition-all duration-300 relative z-10 shrink-0",
                    "group-hover:scale-110",
                    isActive && "scale-110 drop-shadow-[0_0_8px_rgba(59,130,246,0.4)]"
                  )}
                />

                <span
                  ref={(el) => { labelsRef.current[idx] = el; }}
                  className={cn(
                    "text-xs font-semibold transition-all duration-300 relative z-10 tracking-[0.04em] whitespace-nowrap overflow-hidden",
                    isActive && "text-[#3B82F6]"
                  )}
                  style={{ color: isActive ? "#3B82F6" : "var(--theme-text-secondary)" }}
                >
                  {item.label}
                </span>
              </button>
            );
          })}
        </nav>

        {/* Bottom branding */}
        <div className="relative py-4 flex justify-center shrink-0">
          <div className="absolute top-0 left-3 right-3 h-[1px] bg-gradient-to-r from-transparent via-white/[0.04] to-transparent" />
          <span
            className="text-[8px] text-blue-400/20 font-semibold tracking-[0.2em] uppercase whitespace-nowrap"
          >
            MORAA
          </span>
        </div>
      </aside>
    </>
  );
}
