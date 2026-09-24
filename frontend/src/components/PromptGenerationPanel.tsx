"use client";

import { useState, useCallback, useEffect, useRef } from "react";
import { motion } from "framer-motion";
import {
  Sparkles,
  Copy,
  Check,
  AlertTriangle,
  Camera,
  Shirt,
  Palette,
  ShoppingBag,
  PartyPopper,
  Repeat,
  Ruler,
  Smartphone,
  Clock,
  Download,
  RefreshCw,
  ImageIcon,
  Eye,
  Gem,
} from "lucide-react";
import {
  type PromptCategory,
  type PromptGenerationState,
  PROMPT_CATEGORIES,
  PROMPT_CATEGORY_LABELS,
  PROMPT_CATEGORY_DESCRIPTIONS,
} from "@/types/prompts";
import { generatePromptsFromAnalysis } from "@/services/prompt-generation.service";
import { generateImage } from "@/services/image-generation.service";
import { getCleanVisualPrompt } from "@/services/promotional-prompts";
import { generateEarringEcommercePrompt, type EarringType } from "@/services/earring-ecommerce.service";
import { generateCloseUpEarsPrompt } from "@/services/earring-close-up-ears.service";
import { generateScaleReferencePrompt } from "@/services/earring-scale-reference.service";
import { generateProfessionalShotPrompt, type EnvironmentArchetype } from "@/services/earring-professional-shot.service";
import { generateComplementaryShotPrompt } from "@/services/earring-complementary-shot.service";
import { generateUGCStylePrompt } from "@/services/earring-ugc-style.service";
import { generateMacroShotPrompt } from "@/services/earring-macro-shot.service";
import type { AnalysisResult } from "@/types/analysis";
import type { ImageGenerationState } from "@/types/image-generation";
import { logger } from "@/lib/logger";
import PhotoFilterPanel from "@/components/PhotoFilterPanel";

// --- Category Icons and Colors ---

const CATEGORY_ICONS: Record<PromptCategory, React.ElementType> = {
  professionalShot: Camera,
  useCaseShot: Shirt,
  ingredientStory: Palette,
  festive: PartyPopper,
  transformation: Repeat,
  scaleReference: Ruler,
  complementaryShot: ShoppingBag,
  ugcStyle: Smartphone,
};

const CATEGORY_COLORS: Record<PromptCategory, string> = {
  professionalShot: "#3B82F6",
  useCaseShot: "#F59E0B",
  ingredientStory: "#22C55E",
  festive: "#EC4899",
  transformation: "#A855F7",
  scaleReference: "#06B6D4",
  complementaryShot: "#F97316",
  ugcStyle: "#10B981",
};

// --- Animation Variants ---

const containerVariants = {
  hidden: { opacity: 0 },
  show: {
    opacity: 1,
    transition: { staggerChildren: 0.08, delayChildren: 0.1 },
  },
};

const itemVariants = {
  hidden: { opacity: 0, y: 20 },
  show: { opacity: 1, y: 0, transition: { duration: 0.5, ease: [0.16, 1, 0.3, 1] as const } },
};

// --- Provider Label Helper ---

function formatProviderLabel(provider: string, fallbackUsed: boolean, manualSwitch?: boolean): string {
  if (!provider || provider === "none") return "";
  const name = provider.charAt(0).toUpperCase() + provider.slice(1);
  if (manualSwitch) return `${name} · Manual Switch`;
  if (fallbackUsed) return `${name} · Fallback`;
  return name;
}

/** Return the name of the *other* provider for manual switching. */
function getOtherProvider(provider: string): string {
  if (provider === "openai") return "gemini";
  if (provider === "gemini") return "openai";
  return "";
}

/** Capitalize a provider name for button labels. */
function providerDisplayName(provider: string): string {
  if (!provider) return "";
  return provider.charAt(0).toUpperCase() + provider.slice(1);
}

// --- Copy Button ---

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);

  const handleCopy = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      const textarea = document.createElement("textarea");
      textarea.value = text;
      document.body.appendChild(textarea);
      textarea.select();
      document.execCommand("copy");
      document.body.removeChild(textarea);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    }
  }, [text]);

  return (
    <button
      onClick={handleCopy}
      className="flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-[11px] font-semibold transition-all duration-200 hover:bg-white/[0.08] active:scale-95"
      style={{ color: copied ? "#22C55E" : "var(--theme-text-secondary)" }}
    >
      {copied ? (
        <>
          <Check size={12} />
          Copied
        </>
      ) : (
        <>
          <Copy size={12} />
          Copy
        </>
      )}
    </button>
  );
}

// --- Generated Image Display ---

function GeneratedImageDisplay({
  imageState,
  selectedCategory,
  selectedPrompt,
  onRegenerate,
}: {
  imageState: ImageGenerationState & { provider?: string; fallbackUsed?: boolean };
  selectedCategory: PromptCategory | null;
  selectedPrompt: string | null;
  onRegenerate: () => void;
}) {
  const [downloaded, setDownloaded] = useState(false);

  const handleDownload = useCallback(async (downloadUrl: string) => {
    try {
      const response = await fetch(downloadUrl);
      const blob = await response.blob();
      const objectUrl = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = objectUrl;
      const categoryLabel = selectedCategory
        ? PROMPT_CATEGORY_LABELS[selectedCategory].replace(/\s+/g, "_").toLowerCase()
        : "generated";
      a.download = `moraa_gemvision_${categoryLabel}.png`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(objectUrl);
      setDownloaded(true);
      setTimeout(() => setDownloaded(false), 2000);
    } catch (err) {
      logger.error("Failed to download image", { error: String(err) });
    }
  }, [selectedCategory]);

  const isGenerating = imageState.status === "generating";
  const hasImage = imageState.status === "completed" && imageState.imageUrl;

  return (
    <motion.div
      initial={{ opacity: 0, y: 24, scale: 0.97 }}
      animate={{ opacity: 1, y: 0, scale: 1 }}
      transition={{ duration: 0.5, ease: [0.16, 1, 0.3, 1] }}
      className="rounded-2xl border overflow-hidden"
      style={{
        borderColor: "var(--theme-border)",
        backgroundColor: "var(--theme-glass)",
      }}
    >
      {/* Header */}
      <div className="flex items-center justify-between p-4 pb-3">
        <div className="flex items-center gap-3">
          <div className="flex h-8 w-8 items-center justify-center rounded-xl bg-gradient-to-br from-purple-500/20 to-pink-500/20 border border-purple-500/10">
            <ImageIcon size={15} className="text-purple-400" />
          </div>
          <div>
            <h4 className="text-sm font-bold" style={{ color: "var(--theme-text)" }}>
              Generated Image
            </h4>
            {selectedCategory && (
              <p className="text-[10px] mt-0.5" style={{ color: "var(--theme-text-secondary)" }}>
                From: {PROMPT_CATEGORY_LABELS[selectedCategory]}
              </p>
            )}
          </div>
        </div>
      </div>

      {/* Image Area */}
      <div className="px-4 pb-4">
        {isGenerating ? (
          <div className="flex flex-col items-center justify-center py-16 rounded-xl" style={{ backgroundColor: "var(--theme-muted)" }}>
            <div className="relative">
              <div
                className="h-16 w-16 rounded-full border-[3px] animate-spin"
                style={{ borderColor: "var(--theme-border)", borderTopColor: "#A855F7" }}
              />
              <div className="absolute inset-0 flex items-center justify-center">
                <ImageIcon size={20} className="text-purple-400" />
              </div>
            </div>
            <p className="mt-5 text-sm font-medium" style={{ color: "var(--theme-text-secondary)" }}>
              Generating image...
            </p>
            <p className="mt-2 text-[10px]" style={{ color: "var(--theme-text-secondary)", opacity: 0.5 }}>
              Using AI — may take a moment
            </p>
          </div>
        ) : hasImage ? (
          <div className="space-y-4">
            {/* Generated Image — single, centered, large preview */}
            <div className="relative group mx-auto w-full max-w-[640px]">
              <div className="relative overflow-hidden rounded-2xl border" style={{ borderColor: "var(--theme-border-light)" }}>
                {/* eslint-disable-next-line @next/next/no-img-element */}
                <img
                  src={imageState.imageUrl || undefined}
                  alt="Generated product image"
                  className="w-full object-contain"
                  style={{ maxHeight: "560px", minHeight: "240px" }}
                />
                {/* Hover overlay for actions */}
                <div className="absolute inset-0 rounded-2xl bg-black/0 group-hover:bg-black/30 transition-all duration-300 flex items-center justify-center gap-3 opacity-0 group-hover:opacity-100">
                  <button
                    onClick={() => handleDownload(imageState.imageUrl!)}
                    className="flex items-center gap-2 px-4 py-2 rounded-xl bg-white/20 backdrop-blur-md text-white text-xs font-semibold hover:bg-white/30 transition-all duration-200 active:scale-95"
                  >
                    {downloaded ? (<><Check size={14} /> Downloaded</>) : (<><Download size={14} /> Download</>)}
                  </button>
                  <button
                    onClick={onRegenerate}
                    className="flex items-center gap-2 px-4 py-2 rounded-xl bg-white/20 backdrop-blur-md text-white text-xs font-semibold hover:bg-white/30 transition-all duration-200 active:scale-95"
                  >
                    <RefreshCw size={14} /> Regenerate
                  </button>
                </div>
              </div>
            </div>

            {/* Fidelity Check Tip */}
            <div className="rounded-xl p-3 flex items-start gap-3" style={{ backgroundColor: "var(--theme-muted)" }}>
              <Eye size={14} className="text-[#3B82F6] shrink-0 mt-0.5" />
              <div className="text-[11px] leading-relaxed" style={{ color: "var(--theme-text-secondary)" }}>
                <span className="font-semibold" style={{ color: "var(--theme-text)" }}>Fidelity Check:</span>{" "}
                Compare product geometry, stone placement, metalwork, and proportions. The generated image should preserve the exact product design while improving studio lighting and presentation.
              </div>
            </div>

            {/* Bottom info bar */}
            <div className="flex items-center justify-between">
              <span className="text-[10px] font-medium px-2 py-1 rounded-lg" style={{ backgroundColor: "var(--theme-muted)", color: "var(--theme-text-secondary)" }}>
                <Clock size={10} className="inline mr-1" />
                {imageState.generationTime.toFixed(1)}s
              </span>
              <div className="flex items-center gap-2">
                {(() => {
                  const label = formatProviderLabel(imageState.provider || "", imageState.fallbackUsed || false);
                  return label ? (
                    <span className="text-[10px] font-medium px-2 py-1 rounded-lg" style={{ backgroundColor: "var(--theme-muted)", color: "var(--theme-text-secondary)" }}>
                      Generated by {label}
                    </span>
                  ) : null;
                })()}
                {imageState.fallbackUsed && imageState.fallbackReason && (
                  <span className="text-[10px] font-medium px-2 py-1 rounded-lg bg-amber-500/15 text-amber-400">
                    Fallback: {imageState.fallbackReason}
                  </span>
                )}
              </div>
            </div>
          </div>
        ) : imageState.status === "error" ? (
          <div className="flex flex-col items-center justify-center py-12 rounded-xl" style={{ backgroundColor: "var(--theme-muted)" }}>
            <AlertTriangle size={24} className="text-red-400 mb-3" />
            <p className="text-sm font-medium text-red-400/90">Image Generation Failed</p>
            <p className="text-xs mt-1.5 px-6 text-center" style={{ color: "var(--theme-text-secondary)", opacity: 0.7 }}>
              {imageState.errorMessage}
            </p>
            <button
              onClick={onRegenerate}
              className="mt-4 flex items-center gap-2 px-4 py-2 rounded-xl bg-red-500/10 text-red-400 text-xs font-semibold hover:bg-red-500/20 transition-all duration-200 active:scale-95"
            >
              <RefreshCw size={12} />
              Try Again
            </button>
          </div>
        ) : null}
      </div>

      {/* Prompt snippet */}
      {selectedPrompt && (
        <div className="px-4 pb-4">
          <div
            className="rounded-xl p-3 text-[11px] leading-relaxed font-mono line-clamp-2"
            style={{
              backgroundColor: "var(--theme-muted)",
              color: "var(--theme-text-secondary)",
              opacity: 0.6,
            }}
          >
            {selectedPrompt}
          </div>
        </div>
      )}
    </motion.div>
  );
}

// --- Prompt Card ---

function PromptCard({
  category,
  prompt,
  index,
  imageState,
  onGenerate,
}: {
  category: PromptCategory;
  prompt: string;
  index: number;
  imageState: ImageGenerationState;
  onGenerate: (prompt: string, category: PromptCategory, forceProvider?: string) => void;
}) {
  const Icon = CATEGORY_ICONS[category];
  const color = CATEGORY_COLORS[category];
  const label = PROMPT_CATEGORY_LABELS[category];
  const description = PROMPT_CATEGORY_DESCRIPTIONS[category];
  const isGenerating = imageState.status === "generating";
  const hasImage = imageState.status === "completed" && imageState.imageUrl;
  const isError = imageState.status === "error";

  return (
    <motion.div
      variants={itemVariants}
      className="group relative overflow-hidden rounded-2xl border transition-all duration-300 hover:shadow-lg"
      style={{
        borderColor: "var(--theme-border)",
        backgroundColor: "var(--theme-glass)",
      }}
    >
      {/* Top edge color accent */}
      <div
        className="absolute top-0 left-0 right-0 h-[2px] opacity-60"
        style={{ background: `linear-gradient(90deg, ${color}, transparent)` }}
      />

      <div className="p-5">
        {/* Header */}
        <div className="flex items-start justify-between mb-4">
          <div className="flex items-center gap-3">
            <div
              className="flex h-9 w-9 items-center justify-center rounded-xl"
              style={{
                backgroundColor: `${color}15`,
                color: color,
              }}
            >
              <Icon size={16} />
            </div>
            <div>
              <div className="flex items-center gap-2">
                <span className="text-xs font-semibold opacity-40" style={{ color: "var(--theme-text)" }}>
                  {String(index + 1).padStart(2, "0")}
                </span>
                <h4 className="text-sm font-bold" style={{ color: "var(--theme-text)" }}>
                  {label}
                </h4>
              </div>
              <p className="text-[10px] mt-0.5 leading-relaxed max-w-[280px]" style={{ color: "var(--theme-text-secondary)" }}>
                {description}
              </p>
            </div>
          </div>
          <div className="flex items-center gap-2">
            <button
              onClick={() => onGenerate(prompt, category)}
              disabled={isGenerating}
              className="flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-[11px] font-semibold transition-all duration-200 hover:bg-white/[0.08] active:scale-95 disabled:opacity-40 disabled:cursor-not-allowed"
              style={{ color: isGenerating ? color : "var(--theme-text-secondary)" }}
              title={isGenerating ? "Currently generating..." : "Generate image from this prompt"}
            >
              <ImageIcon size={12} />
              {isGenerating ? "Generating..." : "Generate"}
            </button>
            <CopyButton text={prompt} />
          </div>
        </div>

        {/* Prompt Text */}
        <div
          className="rounded-xl p-4 text-[13px] leading-relaxed font-mono whitespace-pre-wrap break-words"
          style={{
            backgroundColor: "var(--theme-muted)",
            color: "var(--theme-text-secondary)",
            borderColor: "var(--theme-border)",
          }}
        >
          {prompt}
        </div>

        {/* Loading State */}
        {isGenerating && (
          <div className="mt-4 flex flex-col items-center justify-center py-8 rounded-xl" style={{ backgroundColor: "var(--theme-muted)" }}>
            <div className="relative">
              <div
                className="h-12 w-12 rounded-full border-[3px] animate-spin"
                style={{ borderColor: "var(--theme-border)", borderTopColor: color }}
              />
              <div className="absolute inset-0 flex items-center justify-center">
                <ImageIcon size={16} style={{ color }} />
              </div>
            </div>
            <p className="mt-3 text-xs font-medium" style={{ color: "var(--theme-text-secondary)" }}>
              Generating {label} image...
            </p>
          </div>
        )}

        {/* Generated Image */}
        {hasImage && (
          <div className="mt-4 space-y-3">
            <div className="relative group mx-auto w-full max-w-[560px]">
              <div className="relative overflow-hidden rounded-xl border" style={{ borderColor: "var(--theme-border-light)" }}>
                {/* eslint-disable-next-line @next/next/no-img-element */}
                <img
                  src={imageState.imageUrl!}
                  alt={`${label} generated image`}
                  className="w-full object-contain"
                  style={{ maxHeight: "480px", minHeight: "200px" }}
                />
              </div>
            </div>
            <div className="flex items-center justify-between">
              <span className="text-[10px] font-medium px-2 py-1 rounded-lg" style={{ backgroundColor: "var(--theme-muted)", color: "var(--theme-text-secondary)" }}>
                <Clock size={10} className="inline mr-1" />
                {imageState.generationTime.toFixed(1)}s
              </span>
              <div className="flex items-center gap-2">
                {(() => {
                  const label = formatProviderLabel(imageState.provider, imageState.fallbackUsed, imageState.manualSwitch);
                  return label ? (
                    <span className="text-[10px] font-medium px-2 py-1 rounded-lg" style={{ backgroundColor: "var(--theme-muted)", color: "var(--theme-text-secondary)" }}>
                      Generated by {label}
                    </span>
                  ) : null;
                })()}
                {imageState.fallbackUsed && imageState.fallbackReason && (
                  <span className="text-[10px] font-medium px-2 py-1 rounded-lg bg-amber-500/15 text-amber-400">
                    Fallback: {imageState.fallbackReason}
                  </span>
                )}
                {(() => {
                  const other = getOtherProvider(imageState.provider);
                  if (!other || isGenerating || !hasImage) return null;
                  return (
                    <button
                      onClick={() => onGenerate(prompt, category, other)}
                      className="flex items-center gap-1 rounded-lg px-2.5 py-1 text-[10px] font-semibold transition-all duration-200 border hover:bg-white/[0.06] active:scale-95"
                      style={{ borderColor: "var(--theme-border)", color: "var(--theme-text-secondary)" }}
                    >
                      <RefreshCw size={9} />
                      Generate with {providerDisplayName(other)}
                    </button>
                  );
                })()}
              </div>
            </div>
          </div>
        )}

        {/* Error State */}
        {isError && (
          <div className="mt-4 flex items-center gap-3 rounded-xl p-3" style={{ backgroundColor: "var(--theme-muted)" }}>
            <AlertTriangle size={14} className="text-red-400 shrink-0" />
            <div className="flex-1 min-w-0">
              <p className="text-[11px] font-medium text-red-400/90">Generation Failed</p>
              <p className="text-[10px] mt-0.5" style={{ color: "var(--theme-text-secondary)", opacity: 0.7 }}>
                {imageState.errorMessage}
              </p>
            </div>
            <button
              onClick={() => onGenerate(prompt, category)}
              className="flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg bg-red-500/10 text-red-400 text-[10px] font-semibold hover:bg-red-500/20 transition-all duration-200 shrink-0"
            >
              <RefreshCw size={10} /> Retry
            </button>
          </div>
        )}
      </div>
    </motion.div>
  );
}

// --- Main Component ---

interface PromptGenerationPanelProps {
  analysisResult: AnalysisResult | null;
  /**
   * Optional image data for fallback backend route.
   * When provided with file, falls back to backend API.
   */
  imageBase64?: string;
  mimeType?: string;
  file?: File;
  onStateChange?: (state: PromptGenerationState) => void;
}

export default function PromptGenerationPanel({
  analysisResult,
  imageBase64,
  mimeType,
  file,
  onStateChange,
}: PromptGenerationPanelProps) {
  const [state, setState] = useState<PromptGenerationState>({
    status: "idle",
    errorMessage: null,
    workflowAnalysis: null,
    prompts: null,
    generationTimeMs: 0,
  });
  const [copyAllStatus, setCopyAllStatus] = useState(false);

  // Earring e-commerce generation state
  const [ecommerceState, setEcommerceState] = useState<ImageGenerationState>({
    status: "idle",
    imageUrl: null,
    provider: "",
    fallbackUsed: false,
    fallbackReason: null,
    generationTime: 0,
    errorMessage: null,
  });
  const [ecommerceEarringType, setEcommerceEarringType] = useState<EarringType | null>(null);
  const [ecommercePreview, setEcommercePreview] = useState<"before" | "after">("after");
  const [ecommerceDownloaded, setEcommerceDownloaded] = useState(false);

  // Close Up Ears generation state (Prompt 2)
  const [closeUpEarsState, setCloseUpEarsState] = useState<ImageGenerationState>({
    status: "idle",
    imageUrl: null,
    provider: "",
    fallbackUsed: false,
    fallbackReason: null,
    generationTime: 0,
    errorMessage: null,
  });
  const [closeUpEarsPreview, setCloseUpEarsPreview] = useState<"before" | "after">("after");
  const [closeUpEarsDownloaded, setCloseUpEarsDownloaded] = useState(false);

  // Scale Reference generation state (Prompt 3)
  const [scaleRefState, setScaleRefState] = useState<ImageGenerationState>({
    status: "idle",
    imageUrl: null,
    provider: "",
    fallbackUsed: false,
    fallbackReason: null,
    generationTime: 0,
    errorMessage: null,
  });
  const [scaleRefEarringType, setScaleRefEarringType] = useState<EarringType | null>(null);
  const [scaleRefPreview, setScaleRefPreview] = useState<"before" | "after">("after");
  const [scaleRefDownloaded, setScaleRefDownloaded] = useState(false);

  // Professional Shot generation state (Prompt 4)
  const [professionalShotState, setProfessionalShotState] = useState<ImageGenerationState>({
    status: "idle",
    imageUrl: null,
    provider: "",
    fallbackUsed: false,
    fallbackReason: null,
    generationTime: 0,
    errorMessage: null,
  });
  const [professionalShotEarringType, setProfessionalShotEarringType] = useState<EarringType | null>(null);
  const [professionalShotArchetype, setProfessionalShotArchetype] = useState<EnvironmentArchetype>("minimalist");
  const [professionalShotPreview, setProfessionalShotPreview] = useState<"before" | "after">("after");
  const [professionalShotDownloaded, setProfessionalShotDownloaded] = useState(false);

  // Complementary Shot generation state (Prompt 5)
  const [complementaryShotState, setComplementaryShotState] = useState<ImageGenerationState>({
    status: "idle",
    imageUrl: null,
    provider: "",
    fallbackUsed: false,
    fallbackReason: null,
    generationTime: 0,
    errorMessage: null,
  });
  const [complementaryShotEarringType, setComplementaryShotEarringType] = useState<EarringType | null>(null);
  const [complementaryShotPreview, setComplementaryShotPreview] = useState<"before" | "after">("after");
  const [complementaryShotDownloaded, setComplementaryShotDownloaded] = useState(false);

  // UGC Style generation state (Prompt 6)
  const [ugcStyleState, setUgcStyleState] = useState<ImageGenerationState>({
    status: "idle",
    imageUrl: null,
    provider: "",
    fallbackUsed: false,
    fallbackReason: null,
    generationTime: 0,
    errorMessage: null,
  });
  const [ugcStyleEarringType, setUgcStyleEarringType] = useState<EarringType | null>(null);
  const [ugcStylePreview, setUgcStylePreview] = useState<"before" | "after">("after");
  const [ugcStyleDownloaded, setUgcStyleDownloaded] = useState(false);

  // Macro Shot generation state (Prompt 7)
  const [macroShotState, setMacroShotState] = useState<ImageGenerationState>({
    status: "idle",
    imageUrl: null,
    provider: "",
    fallbackUsed: false,
    fallbackReason: null,
    generationTime: 0,
    errorMessage: null,
  });
  const [macroShotEarringType, setMacroShotEarringType] = useState<EarringType | null>(null);
  const [macroShotPreview, setMacroShotPreview] = useState<"before" | "after">("after");
  const [macroShotDownloaded, setMacroShotDownloaded] = useState(false);



  // Promotional image generation state — independent per card
  const defaultCardState = (): ImageGenerationState => ({
    status: "idle",
    imageUrl: null,
    provider: "",
    fallbackUsed: false,
    fallbackReason: null,
    generationTime: 0,
    errorMessage: null,
  });
  const [promotionalImages, setPromotionalImages] = useState<Record<PromptCategory, ImageGenerationState>>({
    professionalShot: defaultCardState(),
    useCaseShot: defaultCardState(),
    ingredientStory: defaultCardState(),
    festive: defaultCardState(),
    transformation: defaultCardState(),
    scaleReference: defaultCardState(),
    complementaryShot: defaultCardState(),
    ugcStyle: defaultCardState(),
  });

  // NOTE: setState updaters must stay pure — React may invoke them during the
  // render phase. The onStateChange notification is therefore fired from an
  // effect (after commit) instead of inside the updater, so it can never
  // trigger "Cannot update a component while rendering".
  const updateState = useCallback((partial: Partial<PromptGenerationState>) => {
    setState((prev) => ({ ...prev, ...partial }));
  }, []);

  const lastNotifiedRef = useRef<PromptGenerationState | null>(null);
  useEffect(() => {
    if (!onStateChange) return;
    if (lastNotifiedRef.current === state) return;
    lastNotifiedRef.current = state;
    onStateChange(state);
  }, [state, onStateChange]);

  // ── Prompt Generation (EXISTING — UNCHANGED) ────────────────

  const handleGenerate = useCallback(async () => {
    updateState({ status: "generating", errorMessage: null });

    try {
      if (analysisResult) {
        // PRIMARY PATH: Use existing analysis data + local template engine
        // ZERO Gemini API calls. ZERO tokens consumed.
        logger.info("Generating prompts from existing analysis via local engine", {
          category: analysisResult.category,
        });

        const result = generatePromptsFromAnalysis(analysisResult);

        if (!result.success || !result.data) {
          throw new Error(result.error?.message || "Local prompt generation failed.");
        }

        logger.info("Local prompt generation completed", {
          tokensConsumed: 0,
          generationTimeMs: result.data.generationTimeMs,
          source: "local-template-engine",
        });

        updateState({
          status: "completed",
          workflowAnalysis: result.data.workflowAnalysis,
          prompts: result.data.prompts,
          generationTimeMs: result.data.generationTimeMs,
        });
      } else if (file) {
        // FALLBACK PATH: Backend route (when no analysis data is available)
        logger.info("FALLBACK: Generating prompts via backend (no analysis data provided)");

        const { generatePromptsViaBackend } = await import("@/services/backend-prompt.service");
        const result = await generatePromptsViaBackend(file);

        if (!result.success || !result.data) {
          throw new Error(result.error?.message || "Backend prompt generation failed.");
        }

        updateState({
          status: "completed",
          workflowAnalysis: result.data.workflowAnalysis,
          prompts: result.data.prompts,
          generationTimeMs: result.data.generationTimeMs,
        });
      } else {
        // THIRD PATH: Direct Next.js API route (when base64 data is available)
        logger.info("FALLBACK: Generating prompts via Next.js API route");

        const response = await fetch("/api/prompts/generate", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            imageBase64,
            mimeType,
          }),
        });

        const result = await response.json();

        if (!result.success || !result.data) {
          throw new Error(result.error?.message || "Prompt generation failed.");
        }

        updateState({
          status: "completed",
          workflowAnalysis: result.data.workflowAnalysis,
          prompts: result.data.prompts,
          generationTimeMs: result.data.generationTimeMs,
        });
      }
    } catch (err: unknown) {
      const errMsg = err instanceof Error ? err.message : String(err);
      updateState({
        status: "error",
        errorMessage: errMsg,
      });
    }
  }, [analysisResult, imageBase64, mimeType, file, updateState]);

  const handleCopyAll = useCallback(async () => {
    if (!state.prompts) return;

    const allText = PROMPT_CATEGORIES.map((cat) => {
      const label = PROMPT_CATEGORY_LABELS[cat];
      return `=== ${label} ===\n\n${state.prompts![cat]}`;
    }).join("\n\n---\n\n");

    try {
      await navigator.clipboard.writeText(allText);
      setCopyAllStatus(true);
      setTimeout(() => setCopyAllStatus(false), 2000);
    } catch {
      const textarea = document.createElement("textarea");
      textarea.value = allText;
      document.body.appendChild(textarea);
      textarea.select();
      document.execCommand("copy");
      document.body.removeChild(textarea);
      setCopyAllStatus(true);
      setTimeout(() => setCopyAllStatus(false), 2000);
    }
  }, [state.prompts]);

  // ── Promotional Image Generation (per-card independent state) ──

  const handlePromotionalGenerate = useCallback(async (prompt: string, category: PromptCategory, forceProvider?: string) => {
    // Prevent duplicate API calls for this specific card
    if (promotionalImages[category].status === "generating") return;

    setPromotionalImages((prev) => ({
      ...prev,
      [category]: {
        status: "generating",
        imageUrl: null,
        provider: "openai",
        fallbackUsed: false,
        fallbackReason: null,
        generationTime: 0,
        errorMessage: null,
        manualSwitch: !!forceProvider,
      },
    }));

    try {
      // PREFERRED: Use Prompt 1 clean e-commerce output as product reference
      // FALLBACK: Use raw uploaded reference if Prompt 1 hasn't been run yet
      const refMime = mimeType || "image/jpeg";
      const productRefImage = imageBase64 || ecommerceState.imageUrl;
      const refImageDataUrl = productRefImage
        ? (productRefImage.startsWith("data:") ? productRefImage : `data:${refMime};base64,${productRefImage}`)
        : undefined;

      // Use the clean visual prompt compiler — strips marketing metadata
      // and produces visually actionable instructions for the image model
      const productIdentity = state.workflowAnalysis?.productIdentity || "jewelry earring";
      const cleanPrompt = getCleanVisualPrompt(category, productIdentity);

      // NOTE: NO marketplace overlay — the Amazon India presentation rules
      // enforce pure white background, no human model, and isolated product
      // which directly contradicts the promotional scene compositions.
      const result = await generateImage({
        prompt: cleanPrompt,
        aspectRatio: "4:5",
        referenceImage: refImageDataUrl,
        referenceMimeType: refMime,
        forceProvider,
        // marketplace: intentionally omitted — promotional scenes require
        // lifestyle, workshop, festive, and UGC compositions that the
        // Amazon e-commerce rules would override.
      });

      if (result.success && result.image_url) {
        setPromotionalImages((prev) => ({
          ...prev,
          [category]: {
            status: "completed",
            imageUrl: result.image_url,
            provider: result.provider,
            fallbackUsed: result.fallback_used,
            fallbackReason: result.fallback_reason ?? null,
            generationTime: result.generation_time,
            errorMessage: null,
            manualSwitch: !!forceProvider,
          },
        }));
      } else {
        setPromotionalImages((prev) => ({
          ...prev,
          [category]: {
            status: "error",
            imageUrl: null,
            provider: result.provider,
            fallbackUsed: result.fallback_used,
            fallbackReason: result.fallback_reason ?? null,
            generationTime: result.generation_time,
            errorMessage: result.error || "Image generation failed",
          },
        }));
      }
    } catch (err: unknown) {
      const errMsg = err instanceof Error ? err.message : String(err);
      setPromotionalImages((prev) => ({
        ...prev,
        [category]: {
          status: "error",
          imageUrl: null,
          provider: "none",
          fallbackUsed: false,
          fallbackReason: null,
          generationTime: 0,
          errorMessage: errMsg,
        },
      }));
    }
  }, [promotionalImages, imageBase64, mimeType, ecommerceState.imageUrl, state.workflowAnalysis]);

  // ── Earring E-Commerce Image Generation ────────────────────────────

  const handleEcommerceGenerate = useCallback(async (earringType?: EarringType, forceProvider?: string) => {
    if (ecommerceState.status === "generating") return;

    setEcommerceEarringType(earringType || null);
    setEcommerceState({
      status: "generating",
      imageUrl: null,
      provider: "openai",
      fallbackUsed: false,
      fallbackReason: null,
      generationTime: 0,
      errorMessage: null,
      manualSwitch: !!forceProvider,
    });

    try {
      // Step 1: Get the earring e-commerce prompt from backend
      const prompt = await generateEarringEcommercePrompt({
        earringType: earringType || undefined,
      });

      // Step 2: Send prompt + reference image to generate-image
      const refMime = mimeType || "image/jpeg";
      const result = await generateImage({
        prompt,
        aspectRatio: "4:5",
        referenceImage: imageBase64 ? `data:${refMime};base64,${imageBase64}` : undefined,
        referenceMimeType: refMime,
        marketplace: "amazon_india_fashion_earrings",
        forceProvider,
      });

      if (result.success && result.image_url) {
        setEcommerceState({
          status: "completed",
          imageUrl: result.image_url,
          provider: result.provider,
          fallbackUsed: result.fallback_used,
          fallbackReason: result.fallback_reason ?? null,
          generationTime: result.generation_time,
          errorMessage: null,
          manualSwitch: !!forceProvider,
        });
      } else {
        setEcommerceState({
          status: "error",
          imageUrl: null,
          provider: result.provider,
          fallbackUsed: result.fallback_used,
          fallbackReason: result.fallback_reason ?? null,
          generationTime: result.generation_time,
          errorMessage: result.error || "Earring e-commerce image generation failed",
        });
      }
    } catch (err: unknown) {
      const errMsg = err instanceof Error ? err.message : String(err);
      setEcommerceState({
        status: "error",
        imageUrl: null,
        provider: "none",
        fallbackUsed: false,
        fallbackReason: null,
        generationTime: 0,
        errorMessage: errMsg,
      });
    }
  }, [ecommerceState.status, imageBase64, mimeType]);

  const handleEcommerceDownload = useCallback(async () => {
    if (!ecommerceState.imageUrl) return;

    try {
      const response = await fetch(ecommerceState.imageUrl);
      const blob = await response.blob();
      const objectUrl = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = objectUrl;
      anchor.download = "earring-ecommerce-image.png";
      document.body.appendChild(anchor);
      anchor.click();
      document.body.removeChild(anchor);
      URL.revokeObjectURL(objectUrl);
      setEcommerceDownloaded(true);
      setTimeout(() => setEcommerceDownloaded(false), 2000);
    } catch (err) {
      logger.error("Failed to download earring e-commerce image", { error: String(err) });
    }
  }, [ecommerceState.imageUrl]);

  // ── Close Up Ears Image Generation (Prompt 2) ──────────────────────

  const handleCloseUpEarsGenerate = useCallback(async (forceProvider?: string) => {
    if (closeUpEarsState.status === "generating") return;

    setCloseUpEarsState({
      status: "generating",
      imageUrl: null,
      provider: "openai",
      fallbackUsed: false,
      fallbackReason: null,
      generationTime: 0,
      errorMessage: null,
      manualSwitch: !!forceProvider,
    });

    try {
      // Step 1: Get the Close Up Ears prompt from Prompt 2 backend
      const prompt = await generateCloseUpEarsPrompt();

      // Step 2: Send prompt + reference image to generate-image
      // PREFERRED: Use the Prompt 1 e-commerce output as the product reference
      // because Prompt 1 already produced a cleaned/standardized representation.
      // FALLBACK: Use the raw uploaded reference if Prompt 1 hasn't been run yet.
      const refMime = mimeType || "image/jpeg";
      const productRefImage = imageBase64 || ecommerceState.imageUrl;
      const refImageDataUrl = productRefImage
        ? (productRefImage.startsWith("data:") ? productRefImage : `data:${refMime};base64,${productRefImage}`)
        : undefined;
      const result = await generateImage({
        prompt,
        aspectRatio: "4:5",
        referenceImage: refImageDataUrl,
        referenceMimeType: refMime,
        forceProvider,
        // NOTE: No marketplace overlay — the Amazon India presentation rules
        // enforce pure white background / standalone product which directly
        // contradicts the on-ear composition required by Prompt 2.
      });

      if (result.success && result.image_url) {
        setCloseUpEarsState({
          status: "completed",
          imageUrl: result.image_url,
          provider: result.provider,
          fallbackUsed: result.fallback_used,
          fallbackReason: result.fallback_reason ?? null,
          generationTime: result.generation_time,
          errorMessage: null,
          manualSwitch: !!forceProvider,
        });
      } else {
        setCloseUpEarsState({
          status: "error",
          imageUrl: null,
          provider: result.provider,
          fallbackUsed: result.fallback_used,
          fallbackReason: result.fallback_reason ?? null,
          generationTime: result.generation_time,
          errorMessage: result.error || "Close Up Ears image generation failed",
        });
      }
    } catch (err: unknown) {
      const errMsg = err instanceof Error ? err.message : String(err);
      setCloseUpEarsState({
        status: "error",
        imageUrl: null,
        provider: "none",
        fallbackUsed: false,
        fallbackReason: null,
        generationTime: 0,
        errorMessage: errMsg,
      });
    }
  }, [closeUpEarsState.status, imageBase64, mimeType, ecommerceState.imageUrl]);

  const handleCloseUpEarsDownload = useCallback(async () => {
    if (!closeUpEarsState.imageUrl) return;

    try {
      const response = await fetch(closeUpEarsState.imageUrl);
      const blob = await response.blob();
      const objectUrl = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = objectUrl;
      anchor.download = "earring-close-up-ears.png";
      document.body.appendChild(anchor);
      anchor.click();
      document.body.removeChild(anchor);
      URL.revokeObjectURL(objectUrl);
      setCloseUpEarsDownloaded(true);
      setTimeout(() => setCloseUpEarsDownloaded(false), 2000);
    } catch (err) {
      logger.error("Failed to download Close Up Ears image", { error: String(err) });
    }
  }, [closeUpEarsState.imageUrl]);

  // ── Scale Reference Image Generation (Prompt 3) ─────────────────────

  const handleScaleRefGenerate = useCallback(async (earringType?: EarringType, forceProvider?: string) => {
    if (scaleRefState.status === "generating") return;

    setScaleRefEarringType(earringType || null);
    setScaleRefState({
      status: "generating",
      imageUrl: null,
      provider: "openai",
      fallbackUsed: false,
      fallbackReason: null,
      generationTime: 0,
      errorMessage: null,
      manualSwitch: !!forceProvider,
    });

    try {
      // Step 1: Get the Scale Reference prompt from Prompt 3 backend
      const prompt = await generateScaleReferencePrompt({
        earringType: earringType || undefined,
      });

      // Step 2: Send prompt + reference image to generate-image
      // PREFERRED: Use the Prompt 1 e-commerce output as the product reference
      // FALLBACK: Use the raw uploaded reference if Prompt 1 hasn't been run yet.
      const refMime = mimeType || "image/jpeg";
      const productRefImage = imageBase64 || ecommerceState.imageUrl;
      const refImageDataUrl = productRefImage
        ? (productRefImage.startsWith("data:") ? productRefImage : `data:${refMime};base64,${productRefImage}`)
        : undefined;
      const result = await generateImage({
        prompt,
        aspectRatio: "4:5",
        referenceImage: refImageDataUrl,
        referenceMimeType: refMime,
        forceProvider,
        // NOTE: No marketplace overlay — the Amazon India presentation rules
        // enforce pure white background which aligns with scale reference,
        // but the hand composition may conflict. Omit to let the prompt
        // control the scene.
      });

      if (result.success && result.image_url) {
        setScaleRefState({
          status: "completed",
          imageUrl: result.image_url,
          provider: result.provider,
          fallbackUsed: result.fallback_used,
          fallbackReason: result.fallback_reason ?? null,
          generationTime: result.generation_time,
          errorMessage: null,
          manualSwitch: !!forceProvider,
        });
      } else {
        setScaleRefState({
          status: "error",
          imageUrl: null,
          provider: result.provider,
          fallbackUsed: result.fallback_used,
          fallbackReason: result.fallback_reason ?? null,
          generationTime: result.generation_time,
          errorMessage: result.error || "Scale Reference image generation failed",
        });
      }
    } catch (err: unknown) {
      const errMsg = err instanceof Error ? err.message : String(err);
      setScaleRefState({
        status: "error",
        imageUrl: null,
        provider: "none",
        fallbackUsed: false,
        fallbackReason: null,
        generationTime: 0,
        errorMessage: errMsg,
      });
    }
  }, [scaleRefState.status, imageBase64, mimeType, ecommerceState.imageUrl]);

  const handleScaleRefDownload = useCallback(async () => {
    if (!scaleRefState.imageUrl) return;

    try {
      const response = await fetch(scaleRefState.imageUrl);
      const blob = await response.blob();
      const objectUrl = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = objectUrl;
      anchor.download = "earring-scale-reference.png";
      document.body.appendChild(anchor);
      anchor.click();
      document.body.removeChild(anchor);
      URL.revokeObjectURL(objectUrl);
      setScaleRefDownloaded(true);
      setTimeout(() => setScaleRefDownloaded(false), 2000);
    } catch (err) {
      logger.error("Failed to download Scale Reference image", { error: String(err) });
    }
  }, [scaleRefState.imageUrl]);

  // ── Professional Shot Image Generation (Prompt 4) ──────────────

  const handleProfessionalShotGenerate = useCallback(async (archetype?: EnvironmentArchetype, earringType?: EarringType, forceProvider?: string) => {
    if (professionalShotState.status === "generating") return;

    const selectedArchetype = archetype || "minimalist";
    setProfessionalShotArchetype(selectedArchetype);
    setProfessionalShotEarringType(earringType || null);
    setProfessionalShotState({
      status: "generating",
      imageUrl: null,
      provider: "openai",
      fallbackUsed: false,
      fallbackReason: null,
      generationTime: 0,
      errorMessage: null,
      manualSwitch: !!forceProvider,
    });

    try {
      // Step 1: Get the Professional Commercial Photography prompt from Prompt 4 backend
      const prompt = await generateProfessionalShotPrompt({
        archetype: selectedArchetype,
        earringType: earringType || undefined,
      });

      // Step 2: Send prompt + reference image to generate-image
      // PREFERRED: Use the Prompt 1 e-commerce output as the product reference
      // FALLBACK: Use the raw uploaded reference if Prompt 1 hasn't been run yet.
      const refMime = mimeType || "image/jpeg";
      const productRefImage = imageBase64 || ecommerceState.imageUrl;
      const refImageDataUrl = productRefImage
        ? (productRefImage.startsWith("data:") ? productRefImage : `data:${refMime};base64,${productRefImage}`)
        : undefined;
      const result = await generateImage({
        prompt,
        aspectRatio: "4:5",
        referenceImage: refImageDataUrl,
        referenceMimeType: refMime,
        forceProvider,
        // NOTE: No marketplace overlay — the Amazon India presentation rules
        // are compatible with pure white background, but we let the prompt
        // control the scene for maximum fidelity.
      });

      if (result.success && result.image_url) {
        setProfessionalShotState({
          status: "completed",
          imageUrl: result.image_url,
          provider: result.provider,
          fallbackUsed: result.fallback_used,
          fallbackReason: result.fallback_reason ?? null,
          generationTime: result.generation_time,
          errorMessage: null,
          manualSwitch: !!forceProvider,
        });
      } else {
        setProfessionalShotState({
          status: "error",
          imageUrl: null,
          provider: result.provider,
          fallbackUsed: result.fallback_used,
          fallbackReason: result.fallback_reason ?? null,
          generationTime: result.generation_time,
          errorMessage: result.error || "Professional Commercial Photography image generation failed",
        });
      }
    } catch (err: unknown) {
      const errMsg = err instanceof Error ? err.message : String(err);
      setProfessionalShotState({
        status: "error",
        imageUrl: null,
        provider: "none",
        fallbackUsed: false,
        fallbackReason: null,
        generationTime: 0,
        errorMessage: errMsg,
      });
    }
  }, [professionalShotState.status, professionalShotArchetype, imageBase64, mimeType, ecommerceState.imageUrl]);

  const handleProfessionalShotDownload = useCallback(async () => {
    if (!professionalShotState.imageUrl) return;

    try {
      const response = await fetch(professionalShotState.imageUrl);
      const blob = await response.blob();
      const objectUrl = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = objectUrl;
      anchor.download = "earring-professional-shot.png";
      document.body.appendChild(anchor);
      anchor.click();
      document.body.removeChild(anchor);
      URL.revokeObjectURL(objectUrl);
      setProfessionalShotDownloaded(true);
      setTimeout(() => setProfessionalShotDownloaded(false), 2000);
    } catch (err) {
      logger.error("Failed to download professional shot image", { error: String(err) });
    }
  }, [professionalShotState.imageUrl]);

  // ── Complementary Shot Image Generation (Prompt 5) ──────────────

  const handleComplementaryShotGenerate = useCallback(async (earringType?: EarringType, forceProvider?: string) => {
    if (complementaryShotState.status === "generating") return;

    setComplementaryShotEarringType(earringType || null);
    setComplementaryShotState({
      status: "generating",
      imageUrl: null,
      provider: "openai",
      fallbackUsed: false,
      fallbackReason: null,
      generationTime: 0,
      errorMessage: null,
      manualSwitch: !!forceProvider,
    });

    try {
      // Step 1: Get the Complementary Shot prompt from Prompt 5 backend
      const prompt = await generateComplementaryShotPrompt({
        earringType: earringType || undefined,
      });

      // Step 2: Send prompt + reference image to generate-image
      // PREFERRED: Use the Prompt 1 e-commerce output as the product reference
      // FALLBACK: Use the raw uploaded reference if Prompt 1 hasn't been run yet.
      const refMime = mimeType || "image/jpeg";
      const productRefImage = imageBase64 || ecommerceState.imageUrl;
      const refImageDataUrl = productRefImage
        ? (productRefImage.startsWith("data:") ? productRefImage : `data:${refMime};base64,${productRefImage}`)
        : undefined;
      const result = await generateImage({
        prompt,
        aspectRatio: "4:5",
        referenceImage: refImageDataUrl,
        referenceMimeType: refMime,
        forceProvider,
        // NOTE: No marketplace overlay — the Amazon India presentation rules
        // enforce pure white background which conflicts with editorial staging.
        // Let the prompt control the scene for maximum fidelity.
      });

      if (result.success && result.image_url) {
        setComplementaryShotState({
          status: "completed",
          imageUrl: result.image_url,
          provider: result.provider,
          fallbackUsed: result.fallback_used,
          fallbackReason: result.fallback_reason ?? null,
          generationTime: result.generation_time,
          errorMessage: null,
          manualSwitch: !!forceProvider,
        });
      } else {
        setComplementaryShotState({
          status: "error",
          imageUrl: null,
          provider: result.provider,
          fallbackUsed: result.fallback_used,
          fallbackReason: result.fallback_reason ?? null,
          generationTime: result.generation_time,
          errorMessage: result.error || "Complementary Shot image generation failed",
        });
      }
    } catch (err: unknown) {
      const errMsg = err instanceof Error ? err.message : String(err);
      setComplementaryShotState({
        status: "error",
        imageUrl: null,
        provider: "none",
        fallbackUsed: false,
        fallbackReason: null,
        generationTime: 0,
        errorMessage: errMsg,
      });
    }
  }, [complementaryShotState.status, imageBase64, mimeType, ecommerceState.imageUrl]);

  const handleComplementaryShotDownload = useCallback(async () => {
    if (!complementaryShotState.imageUrl) return;

    try {
      const response = await fetch(complementaryShotState.imageUrl);
      const blob = await response.blob();
      const objectUrl = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = objectUrl;
      anchor.download = "earring-complementary-shot.png";
      document.body.appendChild(anchor);
      anchor.click();
      document.body.removeChild(anchor);
      URL.revokeObjectURL(objectUrl);
      setComplementaryShotDownloaded(true);
      setTimeout(() => setComplementaryShotDownloaded(false), 2000);
    } catch (err) {
      logger.error("Failed to download complementary shot image", { error: String(err) });
    }
  }, [complementaryShotState.imageUrl]);

  // ── UGC Style Image Generation (Prompt 6) ──────────────────────

  const handleUGCStyleGenerate = useCallback(async (earringType?: EarringType, forceProvider?: string) => {
    if (ugcStyleState.status === "generating") return;

    setUgcStyleEarringType(earringType || null);
    setUgcStyleState({
      status: "generating",
      imageUrl: null,
      provider: "openai",
      fallbackUsed: false,
      fallbackReason: null,
      generationTime: 0,
      errorMessage: null,
      manualSwitch: !!forceProvider,
    });

    try {
      // Step 1: Get the UGC Style prompt from Prompt 6 backend
      const prompt = await generateUGCStylePrompt({
        earringType: earringType || undefined,
      });

      // Step 2: Send prompt + reference image to generate-image
      // PREFERRED: Use the Prompt 1 e-commerce output as the product reference
      // FALLBACK: Use the raw uploaded reference if Prompt 1 hasn't been run yet.
      const refMime = mimeType || "image/jpeg";
      const productRefImage = imageBase64 || ecommerceState.imageUrl;
      const refImageDataUrl = productRefImage
        ? (productRefImage.startsWith("data:") ? productRefImage : `data:${refMime};base64,${productRefImage}`)
        : undefined;
      const result = await generateImage({
        prompt,
        aspectRatio: "4:5",
        referenceImage: refImageDataUrl,
        referenceMimeType: refMime,
        forceProvider,
      });

      if (result.success && result.image_url) {
        setUgcStyleState({
          status: "completed",
          imageUrl: result.image_url,
          provider: result.provider,
          fallbackUsed: result.fallback_used,
          fallbackReason: result.fallback_reason ?? null,
          generationTime: result.generation_time,
          errorMessage: null,
          manualSwitch: !!forceProvider,
        });
      } else {
        setUgcStyleState({
          status: "error",
          imageUrl: null,
          provider: result.provider,
          fallbackUsed: result.fallback_used,
          fallbackReason: result.fallback_reason ?? null,
          generationTime: result.generation_time,
          errorMessage: result.error || "UGC Style image generation failed",
        });
      }
    } catch (err: unknown) {
      const errMsg = err instanceof Error ? err.message : String(err);
      setUgcStyleState({
        status: "error",
        imageUrl: null,
        provider: "none",
        fallbackUsed: false,
        fallbackReason: null,
        generationTime: 0,
        errorMessage: errMsg,
      });
    }
  }, [ugcStyleState.status, imageBase64, mimeType, ecommerceState.imageUrl]);

  const handleUGCStyleDownload = useCallback(async () => {
    if (!ugcStyleState.imageUrl) return;

    try {
      const response = await fetch(ugcStyleState.imageUrl);
      const blob = await response.blob();
      const objectUrl = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = objectUrl;
      anchor.download = "earring-ugc-style.png";
      document.body.appendChild(anchor);
      anchor.click();
      document.body.removeChild(anchor);
      URL.revokeObjectURL(objectUrl);
      setUgcStyleDownloaded(true);
      setTimeout(() => setUgcStyleDownloaded(false), 2000);
    } catch (err) {
      logger.error("Failed to download UGC style image", { error: String(err) });
    }
  }, [ugcStyleState.imageUrl]);

  // ── Macro Shot Image Generation (Prompt 7) ──────────────────────

  const handleMacroShotGenerate = useCallback(async (earringType?: EarringType, forceProvider?: string) => {
    if (macroShotState.status === "generating") return;

    setMacroShotEarringType(earringType || null);
    setMacroShotState({
      status: "generating",
      imageUrl: null,
      provider: "openai",
      fallbackUsed: false,
      fallbackReason: null,
      generationTime: 0,
      errorMessage: null,
      manualSwitch: !!forceProvider,
    });

    try {
      // Step 1: Get the Macro Shot prompt from Prompt 7 backend
      const prompt = await generateMacroShotPrompt({
        earringType: earringType || undefined,
      });

      // Step 2: Send prompt + reference image to generate-image
      // PREFERRED: Use the Prompt 1 e-commerce output as the product reference
      // FALLBACK: Use the raw uploaded reference if Prompt 1 hasn't been run yet.
      const refMime = mimeType || "image/jpeg";
      const productRefImage = imageBase64 || ecommerceState.imageUrl;
      const refImageDataUrl = productRefImage
        ? (productRefImage.startsWith("data:") ? productRefImage : `data:${refMime};base64,${productRefImage}`)
        : undefined;
      const result = await generateImage({
        prompt,
        aspectRatio: "4:5",
        referenceImage: refImageDataUrl,
        referenceMimeType: refMime,
        forceProvider,
        enforceQualityFloor: true,
      });

      if (result.success && result.image_url) {
        setMacroShotState({
          status: "completed",
          imageUrl: result.image_url,
          provider: result.provider,
          fallbackUsed: result.fallback_used,
          fallbackReason: result.fallback_reason ?? null,
          generationTime: result.generation_time,
          errorMessage: null,
          manualSwitch: !!forceProvider,
        });
      } else {
        setMacroShotState({
          status: "error",
          imageUrl: null,
          provider: result.provider,
          fallbackUsed: result.fallback_used,
          fallbackReason: result.fallback_reason ?? null,
          generationTime: result.generation_time,
          errorMessage: result.error || "Macro Shot image generation failed",
        });
      }
    } catch (err: unknown) {
      const errMsg = err instanceof Error ? err.message : String(err);
      setMacroShotState({
        status: "error",
        imageUrl: null,
        provider: "none",
        fallbackUsed: false,
        fallbackReason: null,
        generationTime: 0,
        errorMessage: errMsg,
      });
    }
  }, [macroShotState.status, imageBase64, mimeType, ecommerceState.imageUrl]);

  const handleMacroShotDownload = useCallback(async () => {
    if (!macroShotState.imageUrl) return;

    try {
      const response = await fetch(macroShotState.imageUrl);
      const blob = await response.blob();
      const objectUrl = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = objectUrl;
      anchor.download = "earring-macro-shot.png";
      document.body.appendChild(anchor);
      anchor.click();
      document.body.removeChild(anchor);
      URL.revokeObjectURL(objectUrl);
      setMacroShotDownloaded(true);
      setTimeout(() => setMacroShotDownloaded(false), 2000);
    } catch (err) {
      logger.error("Failed to download macro shot image", { error: String(err) });
    }
  }, [macroShotState.imageUrl]);



  // ── Render ──────────────────────────────────────────────────

  const isGenerating = state.status === "generating";
  const hasPrompts = state.status === "completed" && state.prompts !== null;

  return (
    <motion.div
      initial={{ opacity: 0, y: 32 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.5, ease: [0.16, 1, 0.3, 1] }}
      className="space-y-6"
    >
      {/* Section Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <div className="flex h-8 w-8 items-center justify-center rounded-xl neumorphic">
            <Sparkles size={15} className="text-[#3B82F6]" />
          </div>
          <div>
            <h3 className="text-base font-semibold tracking-tight" style={{ color: "var(--theme-text)" }}>
              Promotional Prompts
            </h3>
            <p className="text-xs mt-0.5" style={{ color: "var(--theme-text-secondary)" }}>
              {state.status === "completed"
                ? "Image generation prompts generated from product analysis"
                : "Generate 8 marketing image prompts from the analysis"}
            </p>
          </div>
        </div>

        {hasPrompts && (
          <div className="flex items-center gap-3">
            <span className="text-[10px] font-medium" style={{ color: "var(--theme-text-secondary)" }}>
              <Clock size={10} className="inline mr-1" />
              {(state.generationTimeMs / 1000).toFixed(1)}s
            </span>
            <span className="text-[10px] font-medium px-2 py-0.5 rounded-full bg-emerald-500/10 text-emerald-400">
              0 tokens
            </span>
          </div>
        )}
      </div>

      {/* Generate Button (idle) */}
      {state.status === "idle" && (
        <motion.div variants={itemVariants} className="flex justify-center">
          <button
            onClick={handleGenerate}
            className="group relative flex items-center gap-3 rounded-2xl px-8 py-3.5 text-sm font-semibold text-white overflow-hidden transition-all duration-300 shadow-[0_8px_32px_rgba(59,130,246,0.25)] hover:shadow-[0_8px_40px_rgba(59,130,246,0.35)]"
            style={{
              background: "linear-gradient(135deg, #3B82F6 0%, #1D4ED8 100%)",
            }}
          >
            <span className="absolute inset-0 rounded-2xl pointer-events-none" style={{ background: "linear-gradient(180deg, rgba(255,255,255,0.12) 0%, transparent 50%)" }} />
            <span className="absolute -inset-2 rounded-3xl opacity-0 group-hover:opacity-100 transition-opacity duration-500 pointer-events-none" style={{ background: "radial-gradient(ellipse at center, rgba(59,130,246,0.35) 0%, transparent 70%)", filter: "blur(24px)" }} />
            <span className="relative z-10 flex items-center gap-2.5">
              <Sparkles size={18} className="transition-all duration-300 group-hover:rotate-12 group-hover:scale-110" />
              Generate Promotional Prompts
            </span>
          </button>
        </motion.div>
      )}

      {/* Loading State */}
      {isGenerating && (
        <motion.div
          variants={itemVariants}
          className="flex flex-col items-center justify-center py-12"
        >
          <div className="relative">
            <div
              className="h-16 w-16 rounded-full border-[3px] animate-spin"
              style={{ borderColor: "var(--theme-border)", borderTopColor: "#3B82F6" }}
            />
            <div className="absolute inset-0 flex items-center justify-center">
              <Sparkles size={20} className="text-[#3B82F6]" />
            </div>
          </div>
          <p className="mt-5 text-sm font-medium" style={{ color: "var(--theme-text-secondary)" }}>
            Generating 8 promotional prompts...
          </p>
          <div className="mt-3 flex items-center gap-1.5">
            {[0, 1, 2, 3, 4, 5, 6, 7].map((i) => (
              <motion.div
                key={i}
                className="w-1.5 h-1.5 rounded-full"
                style={{ backgroundColor: "#3B82F6" }}
                animate={{ opacity: [0.2, 1, 0.2] }}
                transition={{ duration: 1.2, repeat: Infinity, delay: i * 0.15 }}
              />
            ))}
          </div>
          <p className="mt-4 text-[10px]" style={{ color: "var(--theme-text-secondary)", opacity: 0.5 }}>
            Using local template engine — 0 API calls, 0 tokens consumed
          </p>
        </motion.div>
      )}

      {/* Error State */}
      {state.status === "error" && (
        <motion.div
          variants={itemVariants}
          className="flex items-start gap-3 rounded-2xl border border-red-500/15 bg-red-500/6 p-5"
        >
          <AlertTriangle size={18} className="text-red-400 shrink-0 mt-0.5" />
          <div className="flex-1 min-w-0">
            <p className="text-xs font-semibold text-red-400/90">Prompt Generation Failed</p>
            <p className="text-xs text-red-400/60 mt-1.5 leading-relaxed">
              {state.errorMessage}
            </p>
            <button
              onClick={handleGenerate}
              className="mt-3 text-xs font-semibold text-[#3B82F6] hover:text-[#60A5FA] transition-colors"
            >
              Try Again
            </button>
          </div>
        </motion.div>
      )}

      {/* Results */}
      {hasPrompts && (
        <motion.div variants={containerVariants} initial="hidden" animate="show" className="space-y-5">
          {/* Summary Bar */}
          <motion.div
            variants={itemVariants}
            className="flex items-center justify-between rounded-2xl border p-3 px-5"
            style={{ borderColor: "var(--theme-border)", backgroundColor: "var(--theme-glass)" }}
          >
            <div className="flex items-center gap-3 text-xs" style={{ color: "var(--theme-text-secondary)" }}>
              <Check size={14} className="text-emerald-400" />
              <span>
                <strong className="text-emerald-400 font-semibold">8/8</strong> prompts generated
              </span>
              <span className="ml-2 px-1.5 py-0.5 rounded text-[9px] font-mono bg-emerald-500/10 text-emerald-400">
                0 tokens
              </span>
            </div>
            <button
              onClick={handleCopyAll}
              className="flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-[11px] font-semibold transition-all duration-200 hover:bg-white/[0.08] active:scale-95"
              style={{ color: copyAllStatus ? "#22C55E" : "var(--theme-text-secondary)" }}
            >
              {copyAllStatus ? (
                <>
                  <Check size={12} />
                  Copied All
                </>
              ) : (
                <>
                  <Copy size={12} />
                  Copy All
                </>
              )}
            </button>
          </motion.div>

          {/* ── Earring E-Commerce Image Generation ──────────────── */}
          <motion.div variants={itemVariants} className="rounded-2xl border overflow-hidden" style={{ borderColor: "var(--theme-border)", backgroundColor: "var(--theme-glass)" }}>
            <div className="p-5">
              <div className="flex items-center gap-3 mb-3">
                <div className="flex h-8 w-8 items-center justify-center rounded-xl bg-gradient-to-br from-emerald-500/20 to-teal-500/20 border border-emerald-500/10">
                  <Gem size={15} className="text-emerald-400" />
                </div>
                <div>
                  <h4 className="text-sm font-bold" style={{ color: "var(--theme-text)" }}>
                    Earring E-Commerce Image
                  </h4>
                  <p className="text-[10px] mt-0.5" style={{ color: "var(--theme-text-secondary)" }}>
                    Single authoritative e-commerce main image for Fashion Jewellery → Earrings
                  </p>
                </div>
              </div>

              {/* Earring Type Selection */}
              <div className="flex items-center gap-2 mb-4">
                <span className="text-[10px] font-semibold uppercase tracking-wider" style={{ color: "var(--theme-text-secondary)" }}>
                  Earring Type:
                </span>
                {(["Hoop", "Stud", "Dangle"] as const).map((type) => (
                  <button
                    key={type}
                    onClick={() => handleEcommerceGenerate(type)}
                    disabled={ecommerceState.status === "generating"}
                    className="rounded-lg px-3 py-1.5 text-[11px] font-semibold transition-all duration-200 border disabled:opacity-40 disabled:cursor-not-allowed"
                    style={{
                      borderColor: ecommerceEarringType === type ? "#10B981" : "var(--theme-border)",
                      backgroundColor: ecommerceEarringType === type ? "#10B981/15" : "var(--theme-muted)",
                      color: ecommerceEarringType === type ? "#10B981" : "var(--theme-text-secondary)",
                    }}
                  >
                    {type}
                  </button>
                ))}
                <button
                  onClick={() => handleEcommerceGenerate()}
                  disabled={ecommerceState.status === "generating"}
                  className="rounded-lg px-3 py-1.5 text-[11px] font-semibold transition-all duration-200 border disabled:opacity-40 disabled:cursor-not-allowed"
                  style={{
                    borderColor: !ecommerceEarringType ? "#10B981" : "var(--theme-border)",
                    backgroundColor: !ecommerceEarringType ? "#10B981/15" : "var(--theme-muted)",
                    color: !ecommerceEarringType ? "#10B981" : "var(--theme-text-secondary)",
                  }}
                >
                  Auto-detect
                </button>
              </div>

              {/* Earring E-Commerce Result */}
              {ecommerceState.status === "generating" && (
                <div className="flex flex-col items-center justify-center py-10 rounded-xl" style={{ backgroundColor: "var(--theme-muted)" }}>
                  <div className="relative">
                    <div className="h-14 w-14 rounded-full border-[3px] animate-spin" style={{ borderColor: "var(--theme-border)", borderTopColor: "#10B981" }} />
                    <div className="absolute inset-0 flex items-center justify-center">
                      <Gem size={18} className="text-emerald-400" />
                    </div>
                  </div>
                  <p className="mt-4 text-sm font-medium" style={{ color: "var(--theme-text-secondary)" }}>
                    Generating earring e-commerce image...
                  </p>
                  <p className="mt-1.5 text-[10px]" style={{ color: "var(--theme-text-secondary)", opacity: 0.5 }}>
                    {ecommerceEarringType ? `${ecommerceEarringType} earring` : "Auto-detecting earring type"} — using AI
                  </p>
                </div>
              )}

              {ecommerceState.status === "completed" && ecommerceState.imageUrl && (
                <div className="space-y-3">
                  <div className="flex items-center justify-between gap-3">
                    <div className="inline-flex rounded-lg p-1" style={{ backgroundColor: "var(--theme-muted)" }}>
                      <button
                        onClick={() => setEcommercePreview("before")}
                        disabled={!imageBase64}
                        className="rounded-md px-3 py-1.5 text-[10px] font-semibold transition-all disabled:cursor-not-allowed disabled:opacity-40"
                        style={{
                          backgroundColor: ecommercePreview === "before" && imageBase64 ? "#10B981" : "transparent",
                          color: ecommercePreview === "before" && imageBase64 ? "white" : "var(--theme-text-secondary)",
                        }}
                      >
                        Before
                      </button>
                      <button
                        onClick={() => setEcommercePreview("after")}
                        className="rounded-md px-3 py-1.5 text-[10px] font-semibold transition-all"
                        style={{
                          backgroundColor: ecommercePreview === "after" ? "#10B981" : "transparent",
                          color: ecommercePreview === "after" ? "white" : "var(--theme-text-secondary)",
                        }}
                      >
                        After
                      </button>
                    </div>
                    <button
                      onClick={handleEcommerceDownload}
                      className="flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-[10px] font-semibold transition-all duration-200 hover:bg-white/[0.08] active:scale-95"
                      style={{ color: ecommerceDownloaded ? "#22C55E" : "var(--theme-text-secondary)" }}
                    >
                      {ecommerceDownloaded ? <><Check size={12} /> Downloaded</> : <><Download size={12} /> Download</>}
                    </button>
                  </div>
                  <div className="relative group mx-auto w-full max-w-[640px]">
                    <div className="relative overflow-hidden rounded-2xl border" style={{ borderColor: "var(--theme-border-light)" }}>
                      {/* eslint-disable-next-line @next/next/no-img-element */}
                      <img
                        src={ecommercePreview === "before" && imageBase64
                          ? `data:${mimeType || "image/jpeg"};base64,${imageBase64}`
                          : ecommerceState.imageUrl}
                        alt={ecommercePreview === "before" ? "Original uploaded earring reference" : "Earring e-commerce generated image"}
                        className="w-full object-contain"
                        style={{ maxHeight: "560px", minHeight: "240px" }}
                      />
                    </div>
                  </div>
                  <div className="flex items-center justify-between">
                    <span className="text-[10px] font-medium px-2 py-1 rounded-lg" style={{ backgroundColor: "var(--theme-muted)", color: "var(--theme-text-secondary)" }}>
                      <Clock size={10} className="inline mr-1" />
                      {ecommerceState.generationTime.toFixed(1)}s
                    </span>
                    <div className="flex items-center gap-2">
                      {(() => {
                        const label = formatProviderLabel(ecommerceState.provider, ecommerceState.fallbackUsed, ecommerceState.manualSwitch);
                        return label ? (
                          <span className="text-[10px] font-medium px-2 py-1 rounded-lg" style={{ backgroundColor: "var(--theme-muted)", color: "var(--theme-text-secondary)" }}>
                            Generated by {label}
                          </span>
                        ) : null;
                      })()}
                      {ecommerceState.fallbackUsed && ecommerceState.fallbackReason && (
                        <span className="text-[10px] font-medium px-2 py-1 rounded-lg bg-amber-500/15 text-amber-400">
                          Fallback: {ecommerceState.fallbackReason}
                        </span>
                      )}
                      {(() => {
                        const other = getOtherProvider(ecommerceState.provider);
                        if (!other) return null;
                        return (
                          <button
                            onClick={() => handleEcommerceGenerate(ecommerceEarringType || undefined, other)}
                            className="flex items-center gap-1 rounded-lg px-2.5 py-1 text-[10px] font-semibold transition-all duration-200 border hover:bg-white/[0.06] active:scale-95"
                            style={{ borderColor: "var(--theme-border)", color: "var(--theme-text-secondary)" }}
                          >
                            <RefreshCw size={9} />
                            Generate with {providerDisplayName(other)}
                          </button>
                        );
                      })()}
                    </div>
                  </div>
                  <div className="rounded-xl p-3 flex items-start gap-3" style={{ backgroundColor: "var(--theme-muted)" }}>
                    <Eye size={14} className="text-emerald-400 shrink-0 mt-0.5" />
                    <div className="text-[11px] leading-relaxed" style={{ color: "var(--theme-text-secondary)" }}>
                      <span className="font-semibold" style={{ color: "var(--theme-text)" }}>Earring E-Commerce:</span>{" "}
                      Generated using the single authoritative earring e-commerce prompt with anti-symmetry, anti-beautification, material fidelity, and input cleanup rules.
                    </div>
                  </div>
                </div>
              )}

              {ecommerceState.status === "error" && (
                <div className="flex flex-col items-center justify-center py-8 rounded-xl" style={{ backgroundColor: "var(--theme-muted)" }}>
                  <AlertTriangle size={20} className="text-red-400 mb-2" />
                  <p className="text-xs font-medium text-red-400/90">Earring E-Commerce Generation Failed</p>
                  <p className="text-[10px] mt-1 px-4 text-center" style={{ color: "var(--theme-text-secondary)", opacity: 0.7 }}>
                    {ecommerceState.errorMessage}
                  </p>
                  <button
                    onClick={() => handleEcommerceGenerate(ecommerceEarringType || undefined)}
                    className="mt-3 flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-red-500/10 text-red-400 text-[10px] font-semibold hover:bg-red-500/20 transition-all duration-200"
                  >
                    <RefreshCw size={10} /> Try Again
                  </button>
                </div>
              )}
            </div>
          </motion.div>

          {/* ── Close Up Ears Image Generation (Prompt 2) ──────────────── */}
          <motion.div variants={itemVariants} className="rounded-2xl border overflow-hidden" style={{ borderColor: "var(--theme-border)", backgroundColor: "var(--theme-glass)" }}>
            <div className="p-5">
              <div className="flex items-center gap-3 mb-3">
                <div className="flex h-8 w-8 items-center justify-center rounded-xl bg-gradient-to-br from-blue-500/20 to-indigo-500/20 border border-blue-500/10">
                  <Eye size={15} className="text-blue-400" />
                </div>
                <div>
                  <h4 className="text-sm font-bold" style={{ color: "var(--theme-text)" }}>
                    Close Up Ears
                  </h4>
                  <p className="text-[10px] mt-0.5" style={{ color: "var(--theme-text-secondary)" }}>
                    On-ear close-up — the exact earring naturally worn on a woman's ear
                  </p>
                </div>
              </div>

              {/* Generate Button */}
              {closeUpEarsState.status === "idle" && (
                <button
                  onClick={() => handleCloseUpEarsGenerate()}
                  disabled={!imageBase64}
                  className="group relative flex items-center gap-2.5 rounded-xl px-5 py-2.5 text-xs font-semibold text-white overflow-hidden transition-all duration-300 disabled:opacity-40 disabled:cursor-not-allowed shadow-[0_4px_20px_rgba(59,130,246,0.2)] hover:shadow-[0_4px_28px_rgba(59,130,246,0.3)]"
                  style={{ background: "linear-gradient(135deg, #3B82F6 0%, #1D4ED8 100%)" }}
                >
                  <span className="absolute inset-0 rounded-xl pointer-events-none" style={{ background: "linear-gradient(180deg, rgba(255,255,255,0.12) 0%, transparent 50%)" }} />
                  <span className="relative z-10 flex items-center gap-2">
                    <Eye size={14} className="transition-all duration-300 group-hover:scale-110" />
                    Generate Close Up Ears
                  </span>
                </button>
              )}

              {/* Loading State */}
              {closeUpEarsState.status === "generating" && (
                <div className="flex flex-col items-center justify-center py-10 rounded-xl" style={{ backgroundColor: "var(--theme-muted)" }}>
                  <div className="relative">
                    <div className="h-14 w-14 rounded-full border-[3px] animate-spin" style={{ borderColor: "var(--theme-border)", borderTopColor: "#3B82F6" }} />
                    <div className="absolute inset-0 flex items-center justify-center">
                      <Eye size={18} className="text-blue-400" />
                    </div>
                  </div>
                  <p className="mt-4 text-sm font-medium" style={{ color: "var(--theme-text-secondary)" }}>
                    Generating Close Up Ears image...
                  </p>
                  <p className="mt-1.5 text-[10px]" style={{ color: "var(--theme-text-secondary)", opacity: 0.5 }}>
                    Prompt 2 — using product reference image
                  </p>
                </div>
              )}

              {/* Result */}
              {closeUpEarsState.status === "completed" && closeUpEarsState.imageUrl && (
                <div className="space-y-3">
                  <div className="flex items-center justify-between gap-3">
                    <div className="inline-flex rounded-lg p-1" style={{ backgroundColor: "var(--theme-muted)" }}>
                      <button
                        onClick={() => setCloseUpEarsPreview("before")}
                        disabled={!imageBase64}
                        className="rounded-md px-3 py-1.5 text-[10px] font-semibold transition-all disabled:cursor-not-allowed disabled:opacity-40"
                        style={{
                          backgroundColor: closeUpEarsPreview === "before" && imageBase64 ? "#3B82F6" : "transparent",
                          color: closeUpEarsPreview === "before" && imageBase64 ? "white" : "var(--theme-text-secondary)",
                        }}
                      >
                        Before
                      </button>
                      <button
                        onClick={() => setCloseUpEarsPreview("after")}
                        className="rounded-md px-3 py-1.5 text-[10px] font-semibold transition-all"
                        style={{
                          backgroundColor: closeUpEarsPreview === "after" ? "#3B82F6" : "transparent",
                          color: closeUpEarsPreview === "after" ? "white" : "var(--theme-text-secondary)",
                        }}
                      >
                        After
                      </button>
                    </div>
                    <button
                      onClick={handleCloseUpEarsDownload}
                      className="flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-[10px] font-semibold transition-all duration-200 hover:bg-white/[0.08] active:scale-95"
                      style={{ color: closeUpEarsDownloaded ? "#22C55E" : "var(--theme-text-secondary)" }}
                    >
                      {closeUpEarsDownloaded ? <><Check size={12} /> Downloaded</> : <><Download size={12} /> Download</>}
                    </button>
                  </div>
                  <div className="relative group mx-auto w-full max-w-[640px]">
                    <div className="relative overflow-hidden rounded-2xl border" style={{ borderColor: "var(--theme-border-light)" }}>
                      {/* eslint-disable-next-line @next/next/no-img-element */}
                      <img
                        src={closeUpEarsPreview === "before" && imageBase64
                          ? `data:${mimeType || "image/jpeg"};base64,${imageBase64}`
                          : closeUpEarsState.imageUrl}
                        alt={closeUpEarsPreview === "before" ? "Original uploaded earring reference" : "Close Up Ears generated image"}
                        className="w-full object-contain"
                        style={{ maxHeight: "560px", minHeight: "240px" }}
                      />
                    </div>
                  </div>
                  <div className="flex items-center justify-between">
                    <span className="text-[10px] font-medium px-2 py-1 rounded-lg" style={{ backgroundColor: "var(--theme-muted)", color: "var(--theme-text-secondary)" }}>
                      <Clock size={10} className="inline mr-1" />
                      {closeUpEarsState.generationTime.toFixed(1)}s
                    </span>
                    <div className="flex items-center gap-2">
                      {(() => {
                        const label = formatProviderLabel(closeUpEarsState.provider, closeUpEarsState.fallbackUsed, closeUpEarsState.manualSwitch);
                        return label ? (
                          <span className="text-[10px] font-medium px-2 py-1 rounded-lg" style={{ backgroundColor: "var(--theme-muted)", color: "var(--theme-text-secondary)" }}>
                            Generated by {label}
                          </span>
                        ) : null;
                      })()}
                      {closeUpEarsState.fallbackUsed && closeUpEarsState.fallbackReason && (
                        <span className="text-[10px] font-medium px-2 py-1 rounded-lg bg-amber-500/15 text-amber-400">
                          Fallback: {closeUpEarsState.fallbackReason}
                        </span>
                      )}
                      {(() => {
                        const other = getOtherProvider(closeUpEarsState.provider);
                        if (!other) return null;
                        return (
                          <button
                            onClick={() => handleCloseUpEarsGenerate(other)}
                            className="flex items-center gap-1 rounded-lg px-2.5 py-1 text-[10px] font-semibold transition-all duration-200 border hover:bg-white/[0.06] active:scale-95"
                            style={{ borderColor: "var(--theme-border)", color: "var(--theme-text-secondary)" }}
                          >
                            <RefreshCw size={9} />
                            Generate with {providerDisplayName(other)}
                          </button>
                        );
                      })()}
                    </div>
                  </div>
                  <div className="rounded-xl p-3 flex items-start gap-3" style={{ backgroundColor: "var(--theme-muted)" }}>
                    <Eye size={14} className="text-blue-400 shrink-0 mt-0.5" />
                    <div className="text-[11px] leading-relaxed" style={{ color: "var(--theme-text-secondary)" }}>
                      <span className="font-semibold" style={{ color: "var(--theme-text)" }}>Close Up Ears:</span>{" "}
                      Generated using Prompt 2 — the exact reference earring naturally worn on a woman's ear in a tight close-up with product fidelity as highest priority.
                    </div>
                  </div>
                  {/* Regenerate button */}
                  <button
                    onClick={() => handleCloseUpEarsGenerate()}
                    className="flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-[10px] font-semibold transition-all duration-200 hover:bg-white/[0.08] active:scale-95"
                    style={{ color: "var(--theme-text-secondary)" }}
                  >
                    <RefreshCw size={10} /> Regenerate
                  </button>
                </div>
              )}

              {/* Error */}
              {closeUpEarsState.status === "error" && (
                <div className="flex flex-col items-center justify-center py-8 rounded-xl" style={{ backgroundColor: "var(--theme-muted)" }}>
                  <AlertTriangle size={20} className="text-red-400 mb-2" />
                  <p className="text-xs font-medium text-red-400/90">Close Up Ears Generation Failed</p>
                  <p className="text-[10px] mt-1 px-4 text-center" style={{ color: "var(--theme-text-secondary)", opacity: 0.7 }}>
                    {closeUpEarsState.errorMessage}
                  </p>
                  <button
                    onClick={() => handleCloseUpEarsGenerate()}
                    className="mt-3 flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-red-500/10 text-red-400 text-[10px] font-semibold hover:bg-red-500/20 transition-all duration-200"
                  >
                    <RefreshCw size={10} /> Try Again
                  </button>
                </div>
              )}
            </div>
          </motion.div>

          {/* ── Scale Reference Image Generation (Prompt 3) ──────────── */}
          <motion.div variants={itemVariants} className="rounded-2xl border overflow-hidden" style={{ borderColor: "var(--theme-border)", backgroundColor: "var(--theme-glass)" }}>
            <div className="p-5">
              <div className="flex items-center gap-3 mb-3">
                <div className="flex h-8 w-8 items-center justify-center rounded-xl bg-gradient-to-br from-cyan-500/20 to-teal-500/20 border border-cyan-500/10">
                  <Ruler size={15} className="text-cyan-400" />
                </div>
                <div>
                  <h4 className="text-sm font-bold" style={{ color: "var(--theme-text)" }}>
                    Scale Reference
                  </h4>
                  <p className="text-[10px] mt-0.5" style={{ color: "var(--theme-text-secondary)" }}>
                    Scale reference — the exact earring with one natural human hand for physical scale
                  </p>
                </div>
              </div>

              {/* Earring Type Selection */}
              <div className="flex items-center gap-2 mb-4">
                <span className="text-[10px] font-semibold uppercase tracking-wider" style={{ color: "var(--theme-text-secondary)" }}>
                  Earring Type:
                </span>
                {(["Hoop", "Stud", "Dangle"] as const).map((type) => (
                  <button
                    key={type}
                    onClick={() => handleScaleRefGenerate(type)}
                    disabled={scaleRefState.status === "generating"}
                    className="rounded-lg px-3 py-1.5 text-[11px] font-semibold transition-all duration-200 border disabled:opacity-40 disabled:cursor-not-allowed"
                    style={{
                      borderColor: scaleRefEarringType === type ? "#06B6D4" : "var(--theme-border)",
                      backgroundColor: scaleRefEarringType === type ? "#06B6D4/15" : "var(--theme-muted)",
                      color: scaleRefEarringType === type ? "#06B6D4" : "var(--theme-text-secondary)",
                    }}
                  >
                    {type}
                  </button>
                ))}
                <button
                  onClick={() => handleScaleRefGenerate()}
                  disabled={scaleRefState.status === "generating"}
                  className="rounded-lg px-3 py-1.5 text-[11px] font-semibold transition-all duration-200 border disabled:opacity-40 disabled:cursor-not-allowed"
                  style={{
                    borderColor: !scaleRefEarringType ? "#06B6D4" : "var(--theme-border)",
                    backgroundColor: !scaleRefEarringType ? "#06B6D4/15" : "var(--theme-muted)",
                    color: !scaleRefEarringType ? "#06B6D4" : "var(--theme-text-secondary)",
                  }}
                >
                  Auto-detect
                </button>
              </div>

              {/* Scale Reference Loading State */}
              {scaleRefState.status === "generating" && (
                <div className="flex flex-col items-center justify-center py-10 rounded-xl" style={{ backgroundColor: "var(--theme-muted)" }}>
                  <div className="relative">
                    <div className="h-14 w-14 rounded-full border-[3px] animate-spin" style={{ borderColor: "var(--theme-border)", borderTopColor: "#06B6D4" }} />
                    <div className="absolute inset-0 flex items-center justify-center">
                      <Ruler size={18} className="text-cyan-400" />
                    </div>
                  </div>
                  <p className="mt-4 text-sm font-medium" style={{ color: "var(--theme-text-secondary)" }}>
                    Generating scale reference image...
                  </p>
                  <p className="mt-1.5 text-[10px]" style={{ color: "var(--theme-text-secondary)", opacity: 0.5 }}>
                    {scaleRefEarringType ? `${scaleRefEarringType} earring` : "Auto-detecting earring type"} — using AI
                  </p>
                </div>
              )}

              {/* Scale Reference Result */}
              {scaleRefState.status === "completed" && scaleRefState.imageUrl && (
                <div className="space-y-3">
                  <div className="flex items-center justify-between gap-3">
                    <div className="inline-flex rounded-lg p-1" style={{ backgroundColor: "var(--theme-muted)" }}>
                      <button
                        onClick={() => setScaleRefPreview("before")}
                        disabled={!imageBase64}
                        className="rounded-md px-3 py-1.5 text-[10px] font-semibold transition-all disabled:cursor-not-allowed disabled:opacity-40"
                        style={{
                          backgroundColor: scaleRefPreview === "before" && imageBase64 ? "#06B6D4" : "transparent",
                          color: scaleRefPreview === "before" && imageBase64 ? "white" : "var(--theme-text-secondary)",
                        }}
                      >
                        Before
                      </button>
                      <button
                        onClick={() => setScaleRefPreview("after")}
                        className="rounded-md px-3 py-1.5 text-[10px] font-semibold transition-all"
                        style={{
                          backgroundColor: scaleRefPreview === "after" ? "#06B6D4" : "transparent",
                          color: scaleRefPreview === "after" ? "white" : "var(--theme-text-secondary)",
                        }}
                      >
                        After
                      </button>
                    </div>
                    <button
                      onClick={handleScaleRefDownload}
                      className="flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-[10px] font-semibold transition-all duration-200 hover:bg-white/[0.08] active:scale-95"
                      style={{ color: scaleRefDownloaded ? "#22C55E" : "var(--theme-text-secondary)" }}
                    >
                      {scaleRefDownloaded ? <><Check size={12} /> Downloaded</> : <><Download size={12} /> Download</>}
                    </button>
                  </div>
                  <div className="relative group mx-auto w-full max-w-[640px]">
                    <div className="relative overflow-hidden rounded-2xl border" style={{ borderColor: "var(--theme-border-light)" }}>
                      {/* eslint-disable-next-line @next/next/no-img-element */}
                      <img
                        src={scaleRefPreview === "before" && imageBase64
                          ? `data:${mimeType || "image/jpeg"};base64,${imageBase64}`
                          : scaleRefState.imageUrl}
                        alt={scaleRefPreview === "before" ? "Original uploaded earring reference" : "Scale Reference generated image"}
                        className="w-full object-contain"
                        style={{ maxHeight: "560px", minHeight: "240px" }}
                      />
                    </div>
                  </div>
                  <div className="flex items-center justify-between">
                    <span className="text-[10px] font-medium px-2 py-1 rounded-lg" style={{ backgroundColor: "var(--theme-muted)", color: "var(--theme-text-secondary)" }}>
                      <Clock size={10} className="inline mr-1" />
                      {scaleRefState.generationTime.toFixed(1)}s
                    </span>
                    <div className="flex items-center gap-2">
                      {(() => {
                        const label = formatProviderLabel(scaleRefState.provider, scaleRefState.fallbackUsed, scaleRefState.manualSwitch);
                        return label ? (
                          <span className="text-[10px] font-medium px-2 py-1 rounded-lg" style={{ backgroundColor: "var(--theme-muted)", color: "var(--theme-text-secondary)" }}>
                            Generated by {label}
                          </span>
                        ) : null;
                      })()}
                      {scaleRefState.fallbackUsed && scaleRefState.fallbackReason && (
                        <span className="text-[10px] font-medium px-2 py-1 rounded-lg bg-amber-500/15 text-amber-400">
                          Fallback: {scaleRefState.fallbackReason}
                        </span>
                      )}
                      {(() => {
                        const other = getOtherProvider(scaleRefState.provider);
                        if (!other) return null;
                        return (
                          <button
                            onClick={() => handleScaleRefGenerate(scaleRefEarringType || undefined, other)}
                            className="flex items-center gap-1 rounded-lg px-2.5 py-1 text-[10px] font-semibold transition-all duration-200 border hover:bg-white/[0.06] active:scale-95"
                            style={{ borderColor: "var(--theme-border)", color: "var(--theme-text-secondary)" }}
                          >
                            <RefreshCw size={9} />
                            Generate with {providerDisplayName(other)}
                          </button>
                        );
                      })()}
                    </div>
                  </div>
                  <div className="rounded-xl p-3 flex items-start gap-3" style={{ backgroundColor: "var(--theme-muted)" }}>
                    <Eye size={14} className="text-cyan-400 shrink-0 mt-0.5" />
                    <div className="text-[11px] leading-relaxed" style={{ color: "var(--theme-text-secondary)" }}>
                      <span className="font-semibold" style={{ color: "var(--theme-text)" }}>Scale Reference:</span>{" "}
                      Generated using Prompt 3 — the exact reference earring shown with one natural human hand as a scale reference on a pure white background, with product fidelity as highest priority.
                    </div>
                  </div>
                  {/* Regenerate button */}
                  <button
                    onClick={() => handleScaleRefGenerate(scaleRefEarringType || undefined)}
                    className="flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-[10px] font-semibold transition-all duration-200 hover:bg-white/[0.08] active:scale-95"
                    style={{ color: "var(--theme-text-secondary)" }}
                  >
                    <RefreshCw size={10} /> Regenerate
                  </button>
                </div>
              )}

              {/* Scale Reference Error */}
              {scaleRefState.status === "error" && (
                <div className="flex flex-col items-center justify-center py-8 rounded-xl" style={{ backgroundColor: "var(--theme-muted)" }}>
                  <AlertTriangle size={20} className="text-red-400 mb-2" />
                  <p className="text-xs font-medium text-red-400/90">Scale Reference Generation Failed</p>
                  <p className="text-[10px] mt-1 px-4 text-center" style={{ color: "var(--theme-text-secondary)", opacity: 0.7 }}>
                    {scaleRefState.errorMessage}
                  </p>
                  <button
                    onClick={() => handleScaleRefGenerate(scaleRefEarringType || undefined)}
                    className="mt-3 flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-red-500/10 text-red-400 text-[10px] font-semibold hover:bg-red-500/20 transition-all duration-200"
                  >
                    <RefreshCw size={10} /> Try Again
                  </button>
                </div>
              )}
            </div>
          </motion.div>

          {/* ── Professional Commercial Photography — Dynamic Environment (Prompt 4) ──────── */}
          <motion.div variants={itemVariants} className="rounded-2xl border overflow-hidden" style={{ borderColor: "var(--theme-border)", backgroundColor: "var(--theme-glass)" }}>
            <div className="p-5">
              <div className="flex items-center gap-3 mb-3">
                <div className="flex h-8 w-8 items-center justify-center rounded-xl bg-gradient-to-br from-violet-500/20 to-purple-500/20 border border-violet-500/10">
                  <Camera size={15} className="text-violet-400" />
                </div>
                <div>
                  <h4 className="text-sm font-bold" style={{ color: "var(--theme-text)" }}>
                    Professional Commercial Photography
                  </h4>
                  <p className="text-[10px] mt-0.5" style={{ color: "var(--theme-text-secondary)" }}>
                    Dynamic environment engine — professional commercial earring photography with selectable studio environments
                  </p>
                </div>
              </div>

              {/* Environment Selection */}
              <div className="flex items-center gap-2 mb-3">
                <span className="text-[10px] font-semibold uppercase tracking-wider" style={{ color: "var(--theme-text-secondary)" }}>
                  Environment:
                </span>
                {([
                  { key: "minimalist" as const, label: "Minimalist Studio" },
                  { key: "organic" as const, label: "Organic Still Life" },
                  { key: "luxury" as const, label: "Luxury Drapery" },
                ]).map(({ key, label }) => (
                  <button
                    key={key}
                    onClick={() => handleProfessionalShotGenerate(key)}
                    disabled={professionalShotState.status === "generating"}
                    className="rounded-lg px-3 py-1.5 text-[11px] font-semibold transition-all duration-200 border disabled:opacity-40 disabled:cursor-not-allowed"
                    style={{
                      borderColor: professionalShotArchetype === key ? "#8B5CF6" : "var(--theme-border)",
                      backgroundColor: professionalShotArchetype === key ? "#8B5CF6/15" : "var(--theme-muted)",
                      color: professionalShotArchetype === key ? "#8B5CF6" : "var(--theme-text-secondary)",
                    }}
                  >
                    {label}
                  </button>
                ))}
              </div>

              {/* Earring Type Selection */}
              <div className="flex items-center gap-2 mb-4">
                <span className="text-[10px] font-semibold uppercase tracking-wider" style={{ color: "var(--theme-text-secondary)" }}>
                  Earring Type:
                </span>
                {(["Hoop", "Stud", "Dangle"] as const).map((type) => (
                  <button
                    key={type}
                    onClick={() => handleProfessionalShotGenerate(professionalShotArchetype, type)}
                    disabled={professionalShotState.status === "generating"}
                    className="rounded-lg px-3 py-1.5 text-[11px] font-semibold transition-all duration-200 border disabled:opacity-40 disabled:cursor-not-allowed"
                    style={{
                      borderColor: professionalShotEarringType === type ? "#8B5CF6" : "var(--theme-border)",
                      backgroundColor: professionalShotEarringType === type ? "#8B5CF6/15" : "var(--theme-muted)",
                      color: professionalShotEarringType === type ? "#8B5CF6" : "var(--theme-text-secondary)",
                    }}
                  >
                    {type}
                  </button>
                ))}
                <button
                  onClick={() => handleProfessionalShotGenerate(professionalShotArchetype)}
                  disabled={professionalShotState.status === "generating"}
                  className="rounded-lg px-3 py-1.5 text-[11px] font-semibold transition-all duration-200 border disabled:opacity-40 disabled:cursor-not-allowed"
                  style={{
                    borderColor: !professionalShotEarringType ? "#8B5CF6" : "var(--theme-border)",
                    backgroundColor: !professionalShotEarringType ? "#8B5CF6/15" : "var(--theme-muted)",
                    color: !professionalShotEarringType ? "#8B5CF6" : "var(--theme-text-secondary)",
                  }}
                >
                  Auto-detect
                </button>
              </div>

              {/* Professional Shot Loading State */}
              {professionalShotState.status === "generating" && (
                <div className="flex flex-col items-center justify-center py-10 rounded-xl" style={{ backgroundColor: "var(--theme-muted)" }}>
                  <div className="relative">
                    <div className="h-14 w-14 rounded-full border-[3px] animate-spin" style={{ borderColor: "var(--theme-border)", borderTopColor: "#8B5CF6" }} />
                    <div className="absolute inset-0 flex items-center justify-center">
                      <Camera size={18} className="text-violet-400" />
                    </div>
                  </div>
                  <p className="mt-4 text-sm font-medium" style={{ color: "var(--theme-text-secondary)" }}>
                    Generating professional commercial image...
                  </p>
                  <p className="mt-1.5 text-[10px]" style={{ color: "var(--theme-text-secondary)", opacity: 0.5 }}>
                    {professionalShotArchetype === "minimalist" ? "Minimalist Studio" : professionalShotArchetype === "organic" ? "Organic Still Life" : "Luxury Drapery"}{professionalShotEarringType ? ` · ${professionalShotEarringType} earring` : ""} — using AI
                  </p>
                </div>
              )}

              {/* Professional Shot Result */}
              {professionalShotState.status === "completed" && professionalShotState.imageUrl && (
                <div className="space-y-3">
                  <div className="flex items-center justify-between gap-3">
                    <div className="inline-flex rounded-lg p-1" style={{ backgroundColor: "var(--theme-muted)" }}>
                      <button
                        onClick={() => setProfessionalShotPreview("before")}
                        disabled={!imageBase64}
                        className="rounded-md px-3 py-1.5 text-[10px] font-semibold transition-all disabled:cursor-not-allowed disabled:opacity-40"
                        style={{
                          backgroundColor: professionalShotPreview === "before" && imageBase64 ? "#8B5CF6" : "transparent",
                          color: professionalShotPreview === "before" && imageBase64 ? "white" : "var(--theme-text-secondary)",
                        }}
                      >
                        Before
                      </button>
                      <button
                        onClick={() => setProfessionalShotPreview("after")}
                        className="rounded-md px-3 py-1.5 text-[10px] font-semibold transition-all"
                        style={{
                          backgroundColor: professionalShotPreview === "after" ? "#8B5CF6" : "transparent",
                          color: professionalShotPreview === "after" ? "white" : "var(--theme-text-secondary)",
                        }}
                      >
                        After
                      </button>
                    </div>
                    <button
                      onClick={handleProfessionalShotDownload}
                      className="flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-[10px] font-semibold transition-all duration-200 hover:bg-white/[0.08] active:scale-95"
                      style={{ color: professionalShotDownloaded ? "#22C55E" : "var(--theme-text-secondary)" }}
                    >
                      {professionalShotDownloaded ? <><Check size={12} /> Downloaded</> : <><Download size={12} /> Download</>}
                    </button>
                  </div>
                  <div className="relative group mx-auto w-full max-w-[640px]">
                    <div className="relative overflow-hidden rounded-2xl border" style={{ borderColor: "var(--theme-border-light)" }}>
                      {/* eslint-disable-next-line @next/next/no-img-element */}
                      <img
                        src={professionalShotPreview === "before" && imageBase64
                          ? `data:${mimeType || "image/jpeg"};base64,${imageBase64}`
                          : professionalShotState.imageUrl}
                        alt={professionalShotPreview === "before" ? "Original uploaded earring reference" : "Professional Shot generated image"}
                        className="w-full object-contain"
                        style={{ maxHeight: "560px", minHeight: "240px" }}
                      />
                    </div>
                  </div>
                  <div className="flex items-center justify-between">
                    <span className="text-[10px] font-medium px-2 py-1 rounded-lg" style={{ backgroundColor: "var(--theme-muted)", color: "var(--theme-text-secondary)" }}>
                      <Clock size={10} className="inline mr-1" />
                      {professionalShotState.generationTime.toFixed(1)}s
                    </span>
                    <div className="flex items-center gap-2">
                      {(() => {
                        const label = formatProviderLabel(professionalShotState.provider, professionalShotState.fallbackUsed, professionalShotState.manualSwitch);
                        return label ? (
                          <span className="text-[10px] font-medium px-2 py-1 rounded-lg" style={{ backgroundColor: "var(--theme-muted)", color: "var(--theme-text-secondary)" }}>
                            Generated by {label}
                          </span>
                        ) : null;
                      })()}
                      {professionalShotState.fallbackUsed && professionalShotState.fallbackReason && (
                        <span className="text-[10px] font-medium px-2 py-1 rounded-lg bg-amber-500/15 text-amber-400">
                          Fallback: {professionalShotState.fallbackReason}
                        </span>
                      )}
                      {(() => {
                        const other = getOtherProvider(professionalShotState.provider);
                        if (!other) return null;
                        return (
                          <button
                            onClick={() => handleProfessionalShotGenerate(professionalShotArchetype, professionalShotEarringType || undefined, other)}
                            className="flex items-center gap-1 rounded-lg px-2.5 py-1 text-[10px] font-semibold transition-all duration-200 border hover:bg-white/[0.06] active:scale-95"
                            style={{ borderColor: "var(--theme-border)", color: "var(--theme-text-secondary)" }}
                          >
                            <RefreshCw size={9} />
                            Generate with {providerDisplayName(other)}
                          </button>
                        );
                      })()}
                    </div>
                  </div>
                  <div className="rounded-xl p-3 flex items-start gap-3" style={{ backgroundColor: "var(--theme-muted)" }}>
                    <Eye size={14} className="text-violet-400 shrink-0 mt-0.5" />
                    <div className="text-[11px] leading-relaxed" style={{ color: "var(--theme-text-secondary)" }}>
                      <span className="font-semibold" style={{ color: "var(--theme-text)" }}>Professional Commercial Photography:</span>{" "}
                      Generated using Prompt 4 — professional commercial earring photography with a dynamic environment engine. The exact jewellery product is preserved with selectable studio environments.
                    </div>
                  </div>
                  {/* Regenerate button */}
                  <button
                    onClick={() => handleProfessionalShotGenerate(professionalShotArchetype, professionalShotEarringType || undefined)}
                    className="flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-[10px] font-semibold transition-all duration-200 hover:bg-white/[0.08] active:scale-95"
                    style={{ color: "var(--theme-text-secondary)" }}
                  >
                    <RefreshCw size={10} /> Regenerate
                  </button>
                </div>
              )}

              {/* Professional Commercial Photography Error */}
              {professionalShotState.status === "error" && (
                <div className="flex flex-col items-center justify-center py-8 rounded-xl" style={{ backgroundColor: "var(--theme-muted)" }}>
                  <AlertTriangle size={20} className="text-red-400 mb-2" />
                  <p className="text-xs font-medium text-red-400/90">Professional Commercial Photography Generation Failed</p>
                  <p className="text-[10px] mt-1 px-4 text-center" style={{ color: "var(--theme-text-secondary)", opacity: 0.7 }}>
                    {professionalShotState.errorMessage}
                  </p>
                  <button
                    onClick={() => handleProfessionalShotGenerate(professionalShotArchetype, professionalShotEarringType || undefined)}
                    className="mt-3 flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-red-500/10 text-red-400 text-[10px] font-semibold hover:bg-red-500/20 transition-all duration-200"
                  >
                    <RefreshCw size={10} /> Try Again
                  </button>
                </div>
              )}
            </div>
          </motion.div>

          {/* ── Complementary Shot Image Generation (Prompt 5) ──────── */}
          <motion.div variants={itemVariants} className="rounded-2xl border overflow-hidden" style={{ borderColor: "var(--theme-border)", backgroundColor: "var(--theme-glass)" }}>
            <div className="p-5">
              <div className="flex items-center gap-3 mb-3">
                <div className="flex h-8 w-8 items-center justify-center rounded-xl bg-gradient-to-br from-amber-500/20 to-orange-500/20 border border-amber-500/10">
                  <ShoppingBag size={15} className="text-amber-400" />
                </div>
                <div>
                  <h4 className="text-sm font-bold" style={{ color: "var(--theme-text)" }}>
                    Complementary Shot
                  </h4>
                  <p className="text-[10px] mt-0.5" style={{ color: "var(--theme-text-secondary)" }}>
                    Lifestyle editorial shot — asymmetric staging & premium contextual environment
                  </p>
                </div>
              </div>

              {/* Earring Type Selection */}
              <div className="flex items-center gap-2 mb-4">
                <span className="text-[10px] font-semibold uppercase tracking-wider" style={{ color: "var(--theme-text-secondary)" }}>
                  Earring Type:
                </span>
                {(["Hoop", "Stud", "Dangle"] as const).map((type) => (
                  <button
                    key={type}
                    onClick={() => handleComplementaryShotGenerate(type)}
                    disabled={complementaryShotState.status === "generating"}
                    className="rounded-lg px-3 py-1.5 text-[11px] font-semibold transition-all duration-200 border disabled:opacity-40 disabled:cursor-not-allowed"
                    style={{
                      borderColor: complementaryShotEarringType === type ? "#F59E0B" : "var(--theme-border)",
                      backgroundColor: complementaryShotEarringType === type ? "#F59E0B/15" : "var(--theme-muted)",
                      color: complementaryShotEarringType === type ? "#F59E0B" : "var(--theme-text-secondary)",
                    }}
                  >
                    {type}
                  </button>
                ))}
                <button
                  onClick={() => handleComplementaryShotGenerate()}
                  disabled={complementaryShotState.status === "generating"}
                  className="rounded-lg px-3 py-1.5 text-[11px] font-semibold transition-all duration-200 border disabled:opacity-40 disabled:cursor-not-allowed"
                  style={{
                    borderColor: !complementaryShotEarringType ? "#F59E0B" : "var(--theme-border)",
                    backgroundColor: !complementaryShotEarringType ? "#F59E0B/15" : "var(--theme-muted)",
                    color: !complementaryShotEarringType ? "#F59E0B" : "var(--theme-text-secondary)",
                  }}
                >
                  Auto-detect
                </button>
              </div>

              {/* Complementary Shot Loading State */}
              {complementaryShotState.status === "generating" && (
                <div className="flex flex-col items-center justify-center py-10 rounded-xl" style={{ backgroundColor: "var(--theme-muted)" }}>
                  <div className="relative">
                    <div className="h-14 w-14 rounded-full border-[3px] animate-spin" style={{ borderColor: "var(--theme-border)", borderTopColor: "#F59E0B" }} />
                    <div className="absolute inset-0 flex items-center justify-center">
                      <ShoppingBag size={18} className="text-amber-400" />
                    </div>
                  </div>
                  <p className="mt-4 text-sm font-medium" style={{ color: "var(--theme-text-secondary)" }}>
                    Generating complementary shot image...
                  </p>
                  <p className="mt-1.5 text-[10px]" style={{ color: "var(--theme-text-secondary)", opacity: 0.5 }}>
                    {complementaryShotEarringType ? `${complementaryShotEarringType} earring` : "Auto-detecting earring type"} — using AI
                  </p>
                </div>
              )}

              {/* Complementary Shot Result */}
              {complementaryShotState.status === "completed" && complementaryShotState.imageUrl && (
                <div className="space-y-3">
                  <div className="flex items-center justify-between gap-3">
                    <div className="inline-flex rounded-lg p-1" style={{ backgroundColor: "var(--theme-muted)" }}>
                      <button
                        onClick={() => setComplementaryShotPreview("before")}
                        disabled={!imageBase64}
                        className="rounded-md px-3 py-1.5 text-[10px] font-semibold transition-all disabled:cursor-not-allowed disabled:opacity-40"
                        style={{
                          backgroundColor: complementaryShotPreview === "before" && imageBase64 ? "#F59E0B" : "transparent",
                          color: complementaryShotPreview === "before" && imageBase64 ? "white" : "var(--theme-text-secondary)",
                        }}
                      >
                        Before
                      </button>
                      <button
                        onClick={() => setComplementaryShotPreview("after")}
                        className="rounded-md px-3 py-1.5 text-[10px] font-semibold transition-all"
                        style={{
                          backgroundColor: complementaryShotPreview === "after" ? "#F59E0B" : "transparent",
                          color: complementaryShotPreview === "after" ? "white" : "var(--theme-text-secondary)",
                        }}
                      >
                        After
                      </button>
                    </div>
                    <button
                      onClick={handleComplementaryShotDownload}
                      className="flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-[10px] font-semibold transition-all duration-200 hover:bg-white/[0.08] active:scale-95"
                      style={{ color: complementaryShotDownloaded ? "#22C55E" : "var(--theme-text-secondary)" }}
                    >
                      {complementaryShotDownloaded ? <><Check size={12} /> Downloaded</> : <><Download size={12} /> Download</>}
                    </button>
                  </div>
                  <div className="relative group mx-auto w-full max-w-[640px]">
                    <div className="relative overflow-hidden rounded-2xl border" style={{ borderColor: "var(--theme-border-light)" }}>
                      {/* eslint-disable-next-line @next/next/no-img-element */}
                      <img
                        src={complementaryShotPreview === "before" && imageBase64
                          ? `data:${mimeType || "image/jpeg"};base64,${imageBase64}`
                          : complementaryShotState.imageUrl}
                        alt={complementaryShotPreview === "before" ? "Original uploaded earring reference" : "Complementary Shot generated image"}
                        className="w-full object-contain"
                        style={{ maxHeight: "560px", minHeight: "240px" }}
                      />
                    </div>
                  </div>
                  <div className="flex items-center justify-between">
                    <span className="text-[10px] font-medium px-2 py-1 rounded-lg" style={{ backgroundColor: "var(--theme-muted)", color: "var(--theme-text-secondary)" }}>
                      <Clock size={10} className="inline mr-1" />
                      {complementaryShotState.generationTime.toFixed(1)}s
                    </span>
                    <div className="flex items-center gap-2">
                      {(() => {
                        const label = formatProviderLabel(complementaryShotState.provider, complementaryShotState.fallbackUsed, complementaryShotState.manualSwitch);
                        return label ? (
                          <span className="text-[10px] font-medium px-2 py-1 rounded-lg" style={{ backgroundColor: "var(--theme-muted)", color: "var(--theme-text-secondary)" }}>
                            Generated by {label}
                          </span>
                        ) : null;
                      })()}
                      {complementaryShotState.fallbackUsed && complementaryShotState.fallbackReason && (
                        <span className="text-[10px] font-medium px-2 py-1 rounded-lg bg-amber-500/15 text-amber-400">
                          Fallback: {complementaryShotState.fallbackReason}
                        </span>
                      )}
                      {(() => {
                        const other = getOtherProvider(complementaryShotState.provider);
                        if (!other) return null;
                        return (
                          <button
                            onClick={() => handleComplementaryShotGenerate(complementaryShotEarringType || undefined, other)}
                            className="flex items-center gap-1 rounded-lg px-2.5 py-1 text-[10px] font-semibold transition-all duration-200 border hover:bg-white/[0.06] active:scale-95"
                            style={{ borderColor: "var(--theme-border)", color: "var(--theme-text-secondary)" }}
                          >
                            <RefreshCw size={9} />
                            Generate with {providerDisplayName(other)}
                          </button>
                        );
                      })()}
                    </div>
                  </div>
                  <div className="rounded-xl p-3 flex items-start gap-3" style={{ backgroundColor: "var(--theme-muted)" }}>
                    <Eye size={14} className="text-amber-400 shrink-0 mt-0.5" />
                    <div className="text-[11px] leading-relaxed" style={{ color: "var(--theme-text-secondary)" }}>
                      <span className="font-semibold" style={{ color: "var(--theme-text)" }}>Complementary Shot:</span>{" "}
                      Generated using Prompt 5 — a lifestyle editorial complementary image with asymmetric staging, premium contextual surface, and storytelling composition, preserving exact product fidelity.
                    </div>
                  </div>
                  {/* Regenerate button */}
                  <button
                    onClick={() => handleComplementaryShotGenerate(complementaryShotEarringType || undefined)}
                    className="flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-[10px] font-semibold transition-all duration-200 hover:bg-white/[0.08] active:scale-95"
                    style={{ color: "var(--theme-text-secondary)" }}
                  >
                    <RefreshCw size={10} /> Regenerate
                  </button>
                </div>
              )}

              {/* Complementary Shot Error */}
              {complementaryShotState.status === "error" && (
                <div className="flex flex-col items-center justify-center py-8 rounded-xl" style={{ backgroundColor: "var(--theme-muted)" }}>
                  <AlertTriangle size={20} className="text-red-400 mb-2" />
                  <p className="text-xs font-medium text-red-400/90">Complementary Shot Generation Failed</p>
                  <p className="text-[10px] mt-1 px-4 text-center" style={{ color: "var(--theme-text-secondary)", opacity: 0.7 }}>
                    {complementaryShotState.errorMessage}
                  </p>
                  <button
                    onClick={() => handleComplementaryShotGenerate(complementaryShotEarringType || undefined)}
                    className="mt-3 flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-red-500/10 text-red-400 text-[10px] font-semibold hover:bg-red-500/20 transition-all duration-200"
                  >
                    <RefreshCw size={10} /> Try Again
                  </button>
                </div>
              )}
            </div>
          </motion.div>

          {/* ── UGC Style Image Generation (Prompt 6) ──────── */}
          <motion.div variants={itemVariants} className="rounded-2xl border overflow-hidden" style={{ borderColor: "var(--theme-border)", backgroundColor: "var(--theme-glass)" }}>
            <div className="p-5">
              <div className="flex items-center gap-3 mb-3">
                <div className="flex h-8 w-8 items-center justify-center rounded-xl bg-gradient-to-br from-emerald-500/20 to-green-500/20 border border-emerald-500/10">
                  <Smartphone size={15} className="text-emerald-400" />
                </div>
                <div>
                  <h4 className="text-sm font-bold" style={{ color: "var(--theme-text)" }}>
                    UGC Style
                  </h4>
                  <p className="text-[10px] mt-0.5" style={{ color: "var(--theme-text-secondary)" }}>
                    Authentic customer perspective — natural daylight, unboxing & vanity lifestyle staging
                  </p>
                </div>
              </div>

              {/* Earring Type Selection */}
              <div className="flex items-center gap-2 mb-4">
                <span className="text-[10px] font-semibold uppercase tracking-wider" style={{ color: "var(--theme-text-secondary)" }}>
                  Earring Type:
                </span>
                {(["Hoop", "Stud", "Dangle"] as const).map((type) => (
                  <button
                    key={type}
                    onClick={() => handleUGCStyleGenerate(type)}
                    disabled={ugcStyleState.status === "generating"}
                    className="rounded-lg px-3 py-1.5 text-[11px] font-semibold transition-all duration-200 border disabled:opacity-40 disabled:cursor-not-allowed"
                    style={{
                      borderColor: ugcStyleEarringType === type ? "#10B981" : "var(--theme-border)",
                      backgroundColor: ugcStyleEarringType === type ? "#10B981/15" : "var(--theme-muted)",
                      color: ugcStyleEarringType === type ? "#10B981" : "var(--theme-text-secondary)",
                    }}
                  >
                    {type}
                  </button>
                ))}
                <button
                  onClick={() => handleUGCStyleGenerate()}
                  disabled={ugcStyleState.status === "generating"}
                  className="rounded-lg px-3 py-1.5 text-[11px] font-semibold transition-all duration-200 border disabled:opacity-40 disabled:cursor-not-allowed"
                  style={{
                    borderColor: !ugcStyleEarringType ? "#10B981" : "var(--theme-border)",
                    backgroundColor: !ugcStyleEarringType ? "#10B981/15" : "var(--theme-muted)",
                    color: !ugcStyleEarringType ? "#10B981" : "var(--theme-text-secondary)",
                  }}
                >
                  Auto-detect
                </button>
              </div>

              {/* UGC Style Loading State */}
              {ugcStyleState.status === "generating" && (
                <div className="flex flex-col items-center justify-center py-10 rounded-xl" style={{ backgroundColor: "var(--theme-muted)" }}>
                  <div className="relative">
                    <div className="h-14 w-14 rounded-full border-[3px] animate-spin" style={{ borderColor: "var(--theme-border)", borderTopColor: "#10B981" }} />
                    <div className="absolute inset-0 flex items-center justify-center">
                      <Smartphone size={18} className="text-emerald-400" />
                    </div>
                  </div>
                  <p className="mt-4 text-sm font-medium" style={{ color: "var(--theme-text-secondary)" }}>
                    Generating UGC style image...
                  </p>
                  <p className="mt-1.5 text-[10px]" style={{ color: "var(--theme-text-secondary)", opacity: 0.5 }}>
                    {ugcStyleEarringType ? `${ugcStyleEarringType} earring` : "Auto-detecting earring type"} — using AI
                  </p>
                </div>
              )}

              {/* UGC Style Result */}
              {ugcStyleState.status === "completed" && ugcStyleState.imageUrl && (
                <div className="space-y-3">
                  <div className="flex items-center justify-between gap-3">
                    <div className="inline-flex rounded-lg p-1" style={{ backgroundColor: "var(--theme-muted)" }}>
                      <button
                        onClick={() => setUgcStylePreview("before")}
                        disabled={!imageBase64}
                        className="rounded-md px-3 py-1.5 text-[10px] font-semibold transition-all disabled:cursor-not-allowed disabled:opacity-40"
                        style={{
                          backgroundColor: ugcStylePreview === "before" && imageBase64 ? "#10B981" : "transparent",
                          color: ugcStylePreview === "before" && imageBase64 ? "white" : "var(--theme-text-secondary)",
                        }}
                      >
                        Before
                      </button>
                      <button
                        onClick={() => setUgcStylePreview("after")}
                        className="rounded-md px-3 py-1.5 text-[10px] font-semibold transition-all"
                        style={{
                          backgroundColor: ugcStylePreview === "after" ? "#10B981" : "transparent",
                          color: ugcStylePreview === "after" ? "white" : "var(--theme-text-secondary)",
                        }}
                      >
                        After
                      </button>
                    </div>
                    <button
                      onClick={handleUGCStyleDownload}
                      className="flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-[10px] font-semibold transition-all duration-200 hover:bg-white/[0.08] active:scale-95"
                      style={{ color: ugcStyleDownloaded ? "#22C55E" : "var(--theme-text-secondary)" }}
                    >
                      {ugcStyleDownloaded ? <><Check size={12} /> Downloaded</> : <><Download size={12} /> Download</>}
                    </button>
                  </div>
                  <div className="relative group mx-auto w-full max-w-[640px]">
                    <div className="relative overflow-hidden rounded-2xl border" style={{ borderColor: "var(--theme-border-light)" }}>
                      {/* eslint-disable-next-line @next/next/no-img-element */}
                      <img
                        src={ugcStylePreview === "before" && imageBase64
                          ? `data:${mimeType || "image/jpeg"};base64,${imageBase64}`
                          : ugcStyleState.imageUrl}
                        alt={ugcStylePreview === "before" ? "Original uploaded earring reference" : "UGC Style generated image"}
                        className="w-full object-contain"
                        style={{ maxHeight: "560px", minHeight: "240px" }}
                      />
                    </div>
                  </div>
                  <div className="flex items-center justify-between">
                    <span className="text-[10px] font-medium px-2 py-1 rounded-lg" style={{ backgroundColor: "var(--theme-muted)", color: "var(--theme-text-secondary)" }}>
                      <Clock size={10} className="inline mr-1" />
                      {ugcStyleState.generationTime.toFixed(1)}s
                    </span>
                    <div className="flex items-center gap-2">
                      {(() => {
                        const label = formatProviderLabel(ugcStyleState.provider, ugcStyleState.fallbackUsed, ugcStyleState.manualSwitch);
                        return label ? (
                          <span className="text-[10px] font-medium px-2 py-1 rounded-lg" style={{ backgroundColor: "var(--theme-muted)", color: "var(--theme-text-secondary)" }}>
                            Generated by {label}
                          </span>
                        ) : null;
                      })()}
                      {ugcStyleState.fallbackUsed && ugcStyleState.fallbackReason && (
                        <span className="text-[10px] font-medium px-2 py-1 rounded-lg bg-amber-500/15 text-amber-400">
                          Fallback: {ugcStyleState.fallbackReason}
                        </span>
                      )}
                      {(() => {
                        const other = getOtherProvider(ugcStyleState.provider);
                        if (!other) return null;
                        return (
                          <button
                            onClick={() => handleUGCStyleGenerate(ugcStyleEarringType || undefined, other)}
                            className="flex items-center gap-1 rounded-lg px-2.5 py-1 text-[10px] font-semibold transition-all duration-200 border hover:bg-white/[0.06] active:scale-95"
                            style={{ borderColor: "var(--theme-border)", color: "var(--theme-text-secondary)" }}
                          >
                            <RefreshCw size={9} />
                            Generate with {providerDisplayName(other)}
                          </button>
                        );
                      })()}
                    </div>
                  </div>
                  <div className="rounded-xl p-3 flex items-start gap-3" style={{ backgroundColor: "var(--theme-muted)" }}>
                    <Eye size={14} className="text-emerald-400 shrink-0 mt-0.5" />
                    <div className="text-[11px] leading-relaxed" style={{ color: "var(--theme-text-secondary)" }}>
                      <span className="font-semibold" style={{ color: "var(--theme-text)" }}>UGC Style:</span>{" "}
                      Generated using Prompt 6 — an authentic customer-perspective UGC lifestyle photograph with natural daylight, believable customer setting, and smartphone photography aesthetic, preserving exact product fidelity.
                    </div>
                  </div>
                  {/* Regenerate button */}
                  <button
                    onClick={() => handleUGCStyleGenerate(ugcStyleEarringType || undefined)}
                    className="flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-[10px] font-semibold transition-all duration-200 hover:bg-white/[0.08] active:scale-95"
                    style={{ color: "var(--theme-text-secondary)" }}
                  >
                    <RefreshCw size={10} /> Regenerate
                  </button>
                </div>
              )}

              {/* UGC Style Error */}
              {ugcStyleState.status === "error" && (
                <div className="flex flex-col items-center justify-center py-8 rounded-xl" style={{ backgroundColor: "var(--theme-muted)" }}>
                  <AlertTriangle size={20} className="text-red-400 mb-2" />
                  <p className="text-xs font-medium text-red-400/90">UGC Style Generation Failed</p>
                  <p className="text-[10px] mt-1 px-4 text-center" style={{ color: "var(--theme-text-secondary)", opacity: 0.7 }}>
                    {ugcStyleState.errorMessage}
                  </p>
                  <button
                    onClick={() => handleUGCStyleGenerate(ugcStyleEarringType || undefined)}
                    className="mt-3 flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-red-500/10 text-red-400 text-[10px] font-semibold hover:bg-red-500/20 transition-all duration-200"
                  >
                    <RefreshCw size={10} /> Try Again
                  </button>
                </div>
              )}
            </div>
          </motion.div>

          {/* ── Macro Shot Image Generation (Prompt 7) ──────── */}
          <motion.div variants={itemVariants} className="rounded-2xl border overflow-hidden" style={{ borderColor: "var(--theme-border)", backgroundColor: "var(--theme-glass)" }}>
            <div className="p-5">
              <div className="flex items-center gap-3 mb-3">
                <div className="flex h-8 w-8 items-center justify-center rounded-xl bg-gradient-to-br from-rose-500/20 to-pink-500/20 border border-rose-500/10">
                  <Gem size={15} className="text-rose-400" />
                </div>
                <div>
                  <h4 className="text-sm font-bold" style={{ color: "var(--theme-text)" }}>
                    Macro Shot
                  </h4>
                  <p className="text-[10px] mt-0.5" style={{ color: "var(--theme-text-secondary)" }}>
                    Macro/detail photograph — genuine fine product details clearly visible
                  </p>
                </div>
              </div>

              {/* Earring Type Selection */}
              <div className="flex items-center gap-2 mb-4">
                <span className="text-[10px] font-semibold uppercase tracking-wider" style={{ color: "var(--theme-text-secondary)" }}>
                  Earring Type:
                </span>
                {(["Hoop", "Stud", "Dangle"] as const).map((type) => (
                  <button
                    key={type}
                    onClick={() => handleMacroShotGenerate(type)}
                    disabled={macroShotState.status === "generating"}
                    className="rounded-lg px-3 py-1.5 text-[11px] font-semibold transition-all duration-200 border disabled:opacity-40 disabled:cursor-not-allowed"
                    style={{
                      borderColor: macroShotEarringType === type ? "#F43F5E" : "var(--theme-border)",
                      backgroundColor: macroShotEarringType === type ? "#F43F5E/15" : "var(--theme-muted)",
                      color: macroShotEarringType === type ? "#F43F5E" : "var(--theme-text-secondary)",
                    }}
                  >
                    {type}
                  </button>
                ))}
                <button
                  onClick={() => handleMacroShotGenerate()}
                  disabled={macroShotState.status === "generating"}
                  className="rounded-lg px-3 py-1.5 text-[11px] font-semibold transition-all duration-200 border disabled:opacity-40 disabled:cursor-not-allowed"
                  style={{
                    borderColor: !macroShotEarringType ? "#F43F5E" : "var(--theme-border)",
                    backgroundColor: !macroShotEarringType ? "#F43F5E/15" : "var(--theme-muted)",
                    color: !macroShotEarringType ? "#F43F5E" : "var(--theme-text-secondary)",
                  }}
                >
                  Auto-detect
                </button>
              </div>

              {/* Macro Shot Loading State */}
              {macroShotState.status === "generating" && (
                <div className="flex flex-col items-center justify-center py-10 rounded-xl" style={{ backgroundColor: "var(--theme-muted)" }}>
                  <div className="relative">
                    <div className="h-14 w-14 rounded-full border-[3px] animate-spin" style={{ borderColor: "var(--theme-border)", borderTopColor: "#F43F5E" }} />
                    <div className="absolute inset-0 flex items-center justify-center">
                      <Gem size={18} className="text-rose-400" />
                    </div>
                  </div>
                  <p className="mt-4 text-sm font-medium" style={{ color: "var(--theme-text-secondary)" }}>
                    Generating macro shot image...
                  </p>
                  <p className="mt-1.5 text-[10px]" style={{ color: "var(--theme-text-secondary)", opacity: 0.5 }}>
                    {macroShotEarringType ? `${macroShotEarringType} earring` : "Auto-detecting earring type"} — using AI
                  </p>
                </div>
              )}

              {/* Macro Shot Result */}
              {macroShotState.status === "completed" && macroShotState.imageUrl && (
                <div className="space-y-3">
                  <div className="flex items-center justify-between gap-3">
                    <div className="inline-flex rounded-lg p-1" style={{ backgroundColor: "var(--theme-muted)" }}>
                      <button
                        onClick={() => setMacroShotPreview("before")}
                        disabled={!imageBase64}
                        className="rounded-md px-3 py-1.5 text-[10px] font-semibold transition-all disabled:cursor-not-allowed disabled:opacity-40"
                        style={{
                          backgroundColor: macroShotPreview === "before" && imageBase64 ? "#F43F5E" : "transparent",
                          color: macroShotPreview === "before" && imageBase64 ? "white" : "var(--theme-text-secondary)",
                        }}
                      >
                        Before
                      </button>
                      <button
                        onClick={() => setMacroShotPreview("after")}
                        className="rounded-md px-3 py-1.5 text-[10px] font-semibold transition-all"
                        style={{
                          backgroundColor: macroShotPreview === "after" ? "#F43F5E" : "transparent",
                          color: macroShotPreview === "after" ? "white" : "var(--theme-text-secondary)",
                        }}
                      >
                        After
                      </button>
                    </div>
                    <button
                      onClick={handleMacroShotDownload}
                      className="flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-[10px] font-semibold transition-all duration-200 hover:bg-white/[0.08] active:scale-95"
                      style={{ color: macroShotDownloaded ? "#22C55E" : "var(--theme-text-secondary)" }}
                    >
                      {macroShotDownloaded ? <><Check size={12} /> Downloaded</> : <><Download size={12} /> Download</>}
                    </button>
                  </div>
                  <div className="relative group mx-auto w-full max-w-[640px]">
                    <div className="relative overflow-hidden rounded-2xl border" style={{ borderColor: "var(--theme-border-light)" }}>
                      {/* eslint-disable-next-line @next/next/no-img-element */}
                      <img
                        src={macroShotPreview === "before" && imageBase64
                          ? `data:${mimeType || "image/jpeg"};base64,${imageBase64}`
                          : macroShotState.imageUrl}
                        alt={macroShotPreview === "before" ? "Original uploaded earring reference" : "Macro Shot generated image"}
                        className="w-full object-contain"
                        style={{ maxHeight: "560px", minHeight: "240px" }}
                      />
                    </div>
                  </div>
                  <div className="flex items-center justify-between">
                    <span className="text-[10px] font-medium px-2 py-1 rounded-lg" style={{ backgroundColor: "var(--theme-muted)", color: "var(--theme-text-secondary)" }}>
                      <Clock size={10} className="inline mr-1" />
                      {macroShotState.generationTime.toFixed(1)}s
                    </span>
                    <div className="flex items-center gap-2">
                      {(() => {
                        const label = formatProviderLabel(macroShotState.provider, macroShotState.fallbackUsed, macroShotState.manualSwitch);
                        return label ? (
                          <span className="text-[10px] font-medium px-2 py-1 rounded-lg" style={{ backgroundColor: "var(--theme-muted)", color: "var(--theme-text-secondary)" }}>
                            Generated by {label}
                          </span>
                        ) : null;
                      })()}
                      {macroShotState.fallbackUsed && macroShotState.fallbackReason && (
                        <span className="text-[10px] font-medium px-2 py-1 rounded-lg bg-amber-500/15 text-amber-400">
                          Fallback: {macroShotState.fallbackReason}
                        </span>
                      )}
                      {(() => {
                        const other = getOtherProvider(macroShotState.provider);
                        if (!other) return null;
                        return (
                          <button
                            onClick={() => handleMacroShotGenerate(macroShotEarringType || undefined, other)}
                            className="flex items-center gap-1 rounded-lg px-2.5 py-1 text-[10px] font-semibold transition-all duration-200 border hover:bg-white/[0.06] active:scale-95"
                            style={{ borderColor: "var(--theme-border)", color: "var(--theme-text-secondary)" }}
                          >
                            <RefreshCw size={9} />
                            Generate with {providerDisplayName(other)}
                          </button>
                        );
                      })()}
                    </div>
                  </div>
                  <div className="rounded-xl p-3 flex items-start gap-3" style={{ backgroundColor: "var(--theme-muted)" }}>
                    <Eye size={14} className="text-rose-400 shrink-0 mt-0.5" />
                    <div className="text-[11px] leading-relaxed" style={{ color: "var(--theme-text-secondary)" }}>
                      <span className="font-semibold" style={{ color: "var(--theme-text)" }}>Macro Shot:</span>{" "}
                      Generated using Prompt 7 — a professional macro/detail photograph showing genuine fine product details (stone settings, cuts, prongs, metal texture, surface finish, edges, joints, hooks, links) with exact product fidelity preserved.
                    </div>
                  </div>
                  {/* Regenerate button */}
                  <button
                    onClick={() => handleMacroShotGenerate(macroShotEarringType || undefined)}
                    className="flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-[10px] font-semibold transition-all duration-200 hover:bg-white/[0.08] active:scale-95"
                    style={{ color: "var(--theme-text-secondary)" }}
                  >
                    <RefreshCw size={10} /> Regenerate
                  </button>
                </div>
              )}

              {/* Macro Shot Error */}
              {macroShotState.status === "error" && (
                <div className="flex flex-col items-center justify-center py-8 rounded-xl" style={{ backgroundColor: "var(--theme-muted)" }}>
                  <AlertTriangle size={20} className="text-red-400 mb-2" />
                  <p className="text-xs font-medium text-red-400/90">Macro Shot Generation Failed</p>
                  <p className="text-[10px] mt-1 px-4 text-center" style={{ color: "var(--theme-text-secondary)", opacity: 0.7 }}>
                    {macroShotState.errorMessage}
                  </p>
                  <button
                    onClick={() => handleMacroShotGenerate(macroShotEarringType || undefined)}
                    className="mt-3 flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-red-500/10 text-red-400 text-[10px] font-semibold hover:bg-red-500/20 transition-all duration-200"
                  >
                    <RefreshCw size={10} /> Try Again
                  </button>
                </div>
              )}
            </div>
          </motion.div>

          {/* Prompt Cards — each with independent image state */}
          {PROMPT_CATEGORIES.map((category, index) => (
            <PromptCard
              key={category}
              category={category}
              prompt={state.prompts![category]}
              index={index}
              imageState={promotionalImages[category]}
              onGenerate={handlePromotionalGenerate}
            />
          ))}

        </motion.div>
      )}
    </motion.div>
  );
}
