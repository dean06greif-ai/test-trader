// Zentraler KI-Modell-Katalog fürs Frontend (eine Quelle für alle Panels).
// Muss zu backend/services/ai_providers.ALLOWED_MODELS passen –
// der Modell-Wächter (services/ai_model_watch.py) verifiziert die Slugs wöchentlich.
export const MODEL_OPTIONS = [
  // Google Gemini (GEMINI_API_KEY)
  { provider: 'gemini', model: 'gemini-3.5-flash', label: 'Gemini 3.5 Flash (Standard, schnell & aktuell)' },
  { provider: 'gemini', model: 'gemini-3.6-flash', label: 'Gemini 3.6 Flash (neuestes Flash)' },
  { provider: 'gemini', model: 'gemini-3.5-flash-lite', label: 'Gemini 3.5 Flash-Lite (sehr günstig)' },
  { provider: 'gemini', model: 'gemini-3.1-pro-preview', label: 'Gemini 3.1 Pro (beste Qualität)' },
  { provider: 'gemini', model: 'gemini-3.1-flash-lite', label: 'Gemini 3.1 Flash-Lite (günstig)' },
  // Groq (GROQ_API_KEY + GROQ_API_KEY_BACKUP) – extrem schnelle Inferenz
  { provider: 'groq', model: 'openai/gpt-oss-120b', label: 'Groq · GPT-OSS 120B (kostenlos, sehr stark)' },
  { provider: 'groq', model: 'llama-3.3-70b-versatile', label: 'Groq · Llama 3.3 70B (kostenlos, stark)' },
  { provider: 'groq', model: 'qwen/qwen3.6-27b', label: 'Groq · Qwen 3.6 27B (kostenlos)' },
  { provider: 'groq', model: 'openai/gpt-oss-20b', label: 'Groq · GPT-OSS 20B (kostenlos, schnell)' },
  { provider: 'groq', model: 'llama-3.1-8b-instant', label: 'Groq · Llama 3.1 8B Instant (kostenlos, blitzschnell)' },
  // OpenRouter (OPENROUTER_API_KEY + Backup) – Free-Katalog live verifiziert
  { provider: 'openrouter', model: 'nvidia/nemotron-3.5-lightning:free', label: 'OpenRouter · Nemotron 3.5 Lightning (kostenlos, neuestes Flaggschiff, 1M Kontext)' },
  { provider: 'openrouter', model: 'deepseek/deepseek-v4-flash', label: 'OpenRouter · DeepSeek V4 Flash (~0,06$/M – bestes Preis/Leistung, bezahlt)' },
  { provider: 'openrouter', model: 'deepseek/deepseek-v4-pro-0813', label: 'OpenRouter · DeepSeek V4 Pro (~0,43$/M – Top-Reasoning, bezahlt)' },
  { provider: 'openrouter', model: 'z-ai/glm-5.2', label: 'OpenRouter · GLM 5.2 (~0,46$/M – stark & präzise, bezahlt)' },
  { provider: 'openrouter', model: 'qwen/qwen3.7-flash', label: 'OpenRouter · Qwen 3.7 Flash (~0,03$/M – ultra-günstig, bezahlt)' },
  { provider: 'openrouter', model: 'x-ai/grok-4.20', label: 'OpenRouter · Grok 4.20 (~1,25$/M – Premium, 2M Kontext, bezahlt)' },
  { provider: 'openrouter', model: 'nvidia/nemotron-3-ultra-550b-a55b:free', label: 'OpenRouter · Nemotron-3 Ultra 550B (kostenlos, beste Qualität)' },
  { provider: 'openrouter', model: 'nvidia/nemotron-3-super-120b-a12b:free', label: 'OpenRouter · Nemotron-3 Super 120B (kostenlos, stark)' },
  { provider: 'openrouter', model: 'google/gemma-4-31b-it:free', label: 'OpenRouter · Gemma 4 31B (kostenlos)' },
  { provider: 'openrouter', model: 'openai/gpt-oss-20b:free', label: 'OpenRouter · GPT-OSS 20B (kostenlos)' },
  { provider: 'openrouter', model: 'nvidia/nemotron-nano-9b-v2:free', label: 'OpenRouter · Nemotron Nano 9B (kostenlos, klein)' },
  // Mistral (MISTRAL_API_KEY)
  { provider: 'mistral', model: 'mistral-small-latest', label: 'Mistral Small (kostenloses Free-Tier)' },
  { provider: 'mistral', model: 'ministral-8b-latest', label: 'Mistral · Ministral 8B (kostenlos)' },
  // Cerebras (CEREBRAS_API_KEY + Backup) – free tier, extrem schnelle Inferenz
  { provider: 'cerebras', model: 'gpt-oss-120b', label: 'Cerebras · GPT-OSS 120B (kostenlos, stark)' },
  { provider: 'cerebras', model: 'zai-glm-4.7', label: 'Cerebras · GLM 4.7 (kostenlos)' },
  { provider: 'cerebras', model: 'gemma-4-31b', label: 'Cerebras · Gemma 4 31B (kostenlos)' },
];

export default MODEL_OPTIONS;
