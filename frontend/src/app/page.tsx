"use client";

import { useState, useEffect, useRef } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { useUIStore } from "@/stores/ui-store";
import { gsap } from "gsap";
import Sidebar from "@/components/Sidebar";
import Header from "@/components/Header";
import DashboardPage from "@/components/pages/DashboardPage";
import NewAnalysisPage from "@/components/pages/NewAnalysisPage";
import PromptEditorPage from "@/components/pages/PromptEditorPage";
// History & Reports pages are kept registered (code intact) but hidden from
// the sidebar — restore by re-adding the nav items.
import HistoryPage from "@/components/pages/HistoryPage";
import ReportsPage from "@/components/pages/ReportsPage";
import SettingsPage from "@/components/pages/SettingsPage";
import ProfilePage from "@/components/pages/ProfilePage";
import ChatsPage from "@/components/pages/ChatsPage";
import FooterBar from "@/components/FooterBar";

const pages: Record<string, React.ReactNode> = {
  "dashboard": <DashboardPage />,
  "new-analysis": <NewAnalysisPage />,
  "prompt-editor": <PromptEditorPage />,
  "history": <HistoryPage />,
  "reports": <ReportsPage />,
  "settings": <SettingsPage />,
  "profile": <ProfilePage />,
  "chats": <ChatsPage />,
};

const pageVariants = {
  initial: { opacity: 0, y: 12 },
  animate: { opacity: 1, y: 0, transition: { duration: 0.3, ease: [0.16, 1, 0.3, 1] as const } },
  exit: { opacity: 0, y: -8, transition: { duration: 0.15 } },
};

export default function Home() {
  const { currentPage, sidebarCollapsed } = useUIStore();
  const mainRef = useRef<HTMLDivElement>(null);

  // Animate main content padding and header left offset based on sidebar state
  useEffect(() => {
    if (!mainRef.current) return;
    const padding = sidebarCollapsed ? 68 : 240;
    gsap.to(mainRef.current, {
      paddingLeft: padding,
      duration: 0.35,
      ease: "power3.inOut",
    });
    // Animate header left position
    const headerEl = document.querySelector("header");
    if (headerEl) {
      gsap.to(headerEl, {
        left: padding,
        duration: 0.35,
        ease: "power3.inOut",
      });
    }
  }, [sidebarCollapsed]);

  // Reset scroll position on page change
  useEffect(() => {
    window.scrollTo({ top: 0, behavior: "smooth" });
  }, [currentPage]);

  return (
    <div className="min-h-screen" style={{ backgroundColor: "var(--theme-bg)" }}>
      {/* ===== Premium Background Layers ===== */}

      {/* Grain/noise texture overlay */}
      <div className="grain-overlay" />

      {/* Vignette - darkens edges for depth */}
      <div className="vignette" />


      {/* Sidebar */}
      <Sidebar />

      {/* Top Navbar */}
      <Header />

      {/* Main Layout */}
      <div
        ref={mainRef}
        className="relative z-10 pt-[60px]"
        style={{ paddingLeft: sidebarCollapsed ? 68 : 240 }}
      >
        <div className="px-6 py-6 mx-auto" style={{ maxWidth: "var(--content-max-width, 1600px)" }}>
          <AnimatePresence mode="wait">
            <motion.div
              key={currentPage}
              variants={pageVariants}
              initial="initial"
              animate="animate"
              exit="exit"
            >
              {pages[currentPage] || <DashboardPage />}
            </motion.div>
          </AnimatePresence>

          {/* Footer - only show on dashboard */}
          {currentPage === "dashboard" && <FooterBar />}
        </div>
      </div>
    </div>
  );
}
