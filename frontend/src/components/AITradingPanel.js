import React, { useState, useEffect, useRef, useCallback } from 'react';
import { Robot, PaperPlaneRight, X, Trash, ArrowsClockwise, Lightning, CaretDown, CaretUp, Newspaper, PushPin, Brain, GraduationCap, CheckCircle, XCircle, Sliders, Coins, Flask, ArrowCounterClockwise, PencilSimple, Crown, Plus, FloppyDisk, Warning, ArrowDown, Prohibit, ChatCircleDots } from '@phosphor-icons/react';
import { toast } from '../lib/toast';
import { authHeaders } from '../auth';
import useInstruments, { assetLabel } from '../hooks/useInstruments';
import useDragScroll from '../hooks/useDragScroll';
import AILabPanel from './AILabPanel';
import AIGovernancePanel from './AIGovernancePanel';
import AIScheduleEditor from './AIScheduleEditor';
import AIStrategyLabPanel from './AIStrategyLabPanel';
import AIQuickPrompts from './AIQuickPrompts';
import AITeamSupervisor from './AITeamSupervisor';
import AIRewardPanel from './AIRewardPanel';
import AITraderReset from './AITraderReset';
import { MODEL_OPTIONS } from '../lib/aiModels';
import './AITradingPanel.css';

const API_URL = process.env.REACT_APP_BACKEND_URL;

// KI-Team: Rollen des KI-Ökosystems (Backend: services/ai_roles.py)
const ROLE_DEFS = [
  { key: 'analyst', label: 'Analyst', desc: 'Regelmäßige Markt-Analysen (Haupt-Loop)' },
  { key: 'deep_analyst', label: 'Tiefen-Analyst', desc: 'Sehr tiefe Analysen zu festen Uhrzeiten' },
  { key: 'research_analyst', label: 'Forschungs-Analyst', desc: 'Wertet Backtests, Optimizer & Regime-Lab aus und lehrt das Team' },
  { key: 'market_observer', label: 'Markt-Beobachter', desc: 'Sammelt laufend Marktzustände als Trainingsdaten' },
  { key: 'trade_manager', label: 'Trade-Manager', desc: 'Eröffnet Trades und steuert sie live: SL/TP, Margin, Hebel, Teil-Close' },
  { key: 'news_watcher', label: 'News-Wächter', desc: 'News + Wirtschaftskalender 24/7' },
  { key: 'chat', label: 'Chat-Assistent', desc: 'Beantwortet deine Anfragen im Chat' },
  { key: 'learner', label: 'Lern-Modul', desc: 'Lektionen aus echten Ergebnissen' },
  { key: 'summarizer', label: 'Tages-Reporter', desc: 'Mitternachts-Zusammenfassung' },
];

const actionClass = (a) => (a === 'LONG' ? 'ai-long' : a === 'SHORT' ? 'ai-short' : 'ai-hold');

// Klartext für Ausfall-Ursachen im Modell-Status (welcher Assistent, warum)
const FAIL_REASON = {
  rate_limited: 'Rate-Limit erreicht',
  error: 'Ausfall (Fehler/Timeout)',
  skipped_too_large: 'Prompt zu groß fürs Token-Budget',
};
const roleLabelOf = (r) => (ROLE_DEFS.find(d => d.key === r)?.label || r || 'unbekannte Rolle');

const AUTONOMY_OPTIONS = [
  { value: 'off', label: 'Aus – KI ändert nichts' },
  { value: 'suggest', label: 'Vorschlagen – du bestätigst' },
  { value: 'auto', label: 'Automatisch – KI passt selbst an' },
];

// Asset-Universum kommt aus /api/coins (Quelle: backend/core/instruments.py)
const coinLabel = assetLabel;
const COIN_STORE_KEY = (coin) => `krypto_ai_chat_coins::${coin || 'BTCUSDT'}`;
const CHAT_FOCUS_STORE_KEY = 'krypto_ai_chat_focus_open';
const CHAT_WINDOW_STORE_KEY = 'krypto_ai_chat_window_open';

// Zwischenspeicher für den KI-Verlauf: beim erneuten Öffnen des Panels ist der
// Verlauf sofort da (der Server-Abruf aktualisiert danach im Hintergrund).
let chatHistoryCache = [];

/** Technische KI-Fehler in eine verständliche Erklärung übersetzen. */
const AI_ERROR_HINTS = [
  [/Kein API-Key|no api key|API_KEY/i,
   'Für die eingestellten KI-Provider ist kein API-Key hinterlegt – die KI kann nicht denken. Trage einen Key in den Server-Umgebungsvariablen ein (z.B. GEMINI_API_KEY).'],
  [/rate.?limit|quota|429|RESOURCE_EXHAUSTED/i,
   'Das Modell hat sein Limit erreicht (zu viele Anfragen/Kontingent aufgebraucht). Die KI weicht automatisch auf ein Fallback-Modell aus; sonst später erneut versuchen oder Intervall erhöhen.'],
  [/location is not supported|FAILED_PRECONDITION/i,
   'Der Server-Standort wird von Google für den Free-Tier gesperrt.'],
  [/timeout|timed out|deadline/i,
   'Das Modell hat zu lange gebraucht (Timeout). Meist vorübergehend – der nächste Zyklus versucht es erneut.'],
  [/401|403|invalid.?api.?key|unauthorized/i,
   'Der API-Key wurde vom Anbieter abgelehnt (ungültig oder abgelaufen). Bitte Key prüfen/erneuern.'],
  [/JSON|parse/i,
   'Die Antwort des Modells war unvollständig/kein gültiges JSON. Die KI verwirft diesen Lauf und versucht es beim nächsten Zyklus erneut.'],
  [/network|connection|ECONN|DNS/i,
   'Netzwerkproblem beim Erreichen des KI-Anbieters. Meist vorübergehend.'],
];

const friendlyAiError = (raw) => {
  const text = String(raw || '');
  const hit = AI_ERROR_HINTS.find(([re]) => re.test(text));
  return hit ? hit[1] : text;
};

const AITradingPanel = ({ onClose, selectedCoin = 'BTCUSDT' }) => {
  const { symbols: ALL_COINS, groups: ASSET_GROUPS } = useInstruments();
  const [status, setStatus] = useState(null);
  const [messages, setMessages] = useState(chatHistoryCache);
  const [input, setInput] = useState('');
  const [streaming, setStreaming] = useState(false);
  const [streamText, setStreamText] = useState('');
  const [analyzing, setAnalyzing] = useState(false);
  const [showSetup, setShowSetup] = useState(false);
  const [tokenUsage, setTokenUsage] = useState(null);
  useEffect(() => {
    if (!showSetup) return;
    fetch(`${API_URL}/api/ai/token-usage?days=7`).then(r => r.json())
      .then(setTokenUsage).catch(() => {});
  }, [showSetup]);
  const [showHealth, setShowHealth] = useState(false);
  // Sprung-Pfeil im KI-Verlauf: sichtbar, sobald der Nutzer hochgescrollt hat
  const [showJump, setShowJump] = useState(false);
  // Aufgeklappte Analyse-Begründungen (pro Coin die komplette Begründung lesen)
  const [expandedDecs, setExpandedDecs] = useState({});
  // Coin-Auswahl für den Chat-Kontext (Feature: Coin-spezifischer KI-Chat)
  const [chatCoins, setChatCoins] = useState([selectedCoin]);
  const [proposals, setProposals] = useState([]);
  // Aktueller Status als Ref, damit loadProposals (Intervall) den Autonomie-Modus kennt
  const statusRef = useRef(null);
  const [insights, setInsights] = useState(null);
  const [feeGuardStats, setFeeGuardStats] = useState(null);
  const [showLearn, setShowLearn] = useState(false);
  const [learning, setLearning] = useState(false);
  // KI-Team (Rollen-Konfiguration)
  const [roles, setRoles] = useState(null);
  const [showTeam, setShowTeam] = useState(false);
  const [showLab, setShowLab] = useState(false);
  const [showGov, setShowGov] = useState(false);
  const [showStrat, setShowStrat] = useState(false);
  // KI-Chat als vollwertiges Fenster (wie MasterPrompt): Toggle in der
  // Status-Row, Zustand persistiert – nicht mehr dauerhaft eingebettet.
  const [showChat, setShowChat] = useState(() => {
    try { return localStorage.getItem(CHAT_WINDOW_STORE_KEY) === '1'; } catch (e) { return false; }
  });
  const persistChatWindow = (open) => {
    try { localStorage.setItem(CHAT_WINDOW_STORE_KEY, open ? '1' : '0'); } catch (e) { /* ignore */ }
  };
  const closeChat = () => { setShowChat(false); persistChatWindow(false); };
  const toggleChat = () => {
    setShowChat(prev => {
      const next = !prev;
      persistChatWindow(next);
      if (next) { setShowLearn(false); setShowSetup(false); setShowTeam(false); setShowLab(false); setShowGov(false); setShowStrat(false); }
      return next;
    });
  };
  // Lektionen bearbeiten (Trader-Rechte: Stift / Löschen / Hinzufügen)
  const [editLesson, setEditLesson] = useState(null);
  const [newLesson, setNewLesson] = useState(null);
  const [deepRunning, setDeepRunning] = useState(false);
  // Einzeilige Leisten: seitwärts wischen (Touch nativ, Maus per Ziehen)
  const statusDrag = useDragScroll();
  const proposalDrag = useDragScroll();
  const focusDrag = useDragScroll({ wheel: false });
  // "Coin-Fokus"-Bereich ein-/ausklappbar (Standard: eingeklappt, persistiert in localStorage)
  const [showChatFocus, setShowChatFocus] = useState(() => {
    try { return localStorage.getItem(CHAT_FOCUS_STORE_KEY) === '1'; } catch (e) { return false; }
  });

  // Entweder-Oder: beim Öffnen eines Panels werden die anderen geschlossen.
  const closeChatFocus = () => {
    setShowChatFocus(false);
    try { localStorage.setItem(CHAT_FOCUS_STORE_KEY, '0'); } catch (e) { /* ignore */ }
  };
  const toggleChatFocus = () => {
    setShowChatFocus(prev => {
      const next = !prev;
      try { localStorage.setItem(CHAT_FOCUS_STORE_KEY, next ? '1' : '0'); } catch (e) { /* ignore */ }
      if (next) { setShowLearn(false); setShowSetup(false); setShowGov(false); setShowStrat(false); }
      return next;
    });
  };
  const toggleLearn = () => {
    setShowLearn(prev => {
      const next = !prev;
      if (next) { loadInsights(); setShowSetup(false); setShowTeam(false); setShowLab(false); setShowGov(false); setShowStrat(false); closeChatFocus(); closeChat(); }
      return next;
    });
  };
  const toggleSetup = () => {
    setShowSetup(prev => {
      const next = !prev;
      if (next) { setShowLearn(false); setShowTeam(false); setShowLab(false); setShowGov(false); setShowStrat(false); closeChatFocus(); closeChat(); }
      return next;
    });
  };
  const toggleTeam = () => {
    setShowTeam(prev => {
      const next = !prev;
      if (next) { setShowLearn(false); setShowSetup(false); setShowLab(false); setShowGov(false); setShowStrat(false); closeChatFocus(); closeChat(); }
      return next;
    });
  };
  const toggleLab = () => {
    setShowLab(prev => {
      const next = !prev;
      if (next) { setShowLearn(false); setShowSetup(false); setShowTeam(false); setShowGov(false); setShowStrat(false); closeChatFocus(); closeChat(); }
      return next;
    });
  };
  const toggleGov = () => {
    setShowGov(prev => {
      const next = !prev;
      if (next) { setShowLearn(false); setShowSetup(false); setShowTeam(false); setShowLab(false); setShowStrat(false); closeChatFocus(); closeChat(); }
      return next;
    });
  };
  const toggleStrat = () => {
    setShowStrat(prev => {
      const next = !prev;
      if (next) { setShowLearn(false); setShowSetup(false); setShowTeam(false); setShowLab(false); setShowGov(false); closeChatFocus(); closeChat(); }
      return next;
    });
  };
  const chatEndRef = useRef(null);
  const chatAreaRef = useRef(null);
  const atBottomRef = useRef(true);
  const streamingRef = useRef(false);

  // Chip-Reihenfolge: aktueller Coin immer vorne, danach der Rest.
  const orderedCoins = React.useMemo(
    () => [selectedCoin, ...ALL_COINS.filter(c => c !== selectedCoin)],
    [selectedCoin, ALL_COINS],
  );
  const allSelected = ALL_COINS.length > 0 && chatCoins.length >= ALL_COINS.length;
  // Kompakte Anzeige der aktuellen Auswahl (für Button-Text & Tooltip)
  const focusSummary = allSelected ? 'alle Assets' : chatCoins.map(coinLabel).join(', ');

  // Beim Öffnen / Asset-Wechsel: gespeicherte Auswahl je Asset-Ansicht laden,
  // sonst standardmäßig ALLE Assets vorwählen (Voreinstellung: alle Assets).
  useEffect(() => {
    let next = ALL_COINS.length ? [...ALL_COINS] : [selectedCoin];
    try {
      const raw = localStorage.getItem(COIN_STORE_KEY(selectedCoin));
      if (raw) {
        const parsed = JSON.parse(raw);
        if (Array.isArray(parsed) && parsed.length) {
          next = ALL_COINS.length ? parsed.filter(c => ALL_COINS.includes(c)) : parsed;
          if (!next.length) next = [selectedCoin];
        }
      }
    } catch (e) { /* ignore */ }
    setChatCoins(next);
  }, [selectedCoin, ALL_COINS]);

  const persistCoins = (coins) => {
    setChatCoins(coins);
    try { localStorage.setItem(COIN_STORE_KEY(selectedCoin), JSON.stringify(coins)); } catch (e) { /* ignore */ }
  };

  const toggleCoin = (coin) => {
    const has = chatCoins.includes(coin);
    let next = has ? chatCoins.filter(c => c !== coin) : [...chatCoins, coin];
    if (!next.length) next = [selectedCoin]; // mind. ein Coin bleibt aktiv
    persistCoins(next);
  };

  const loadStatus = useCallback(async () => {
    try {
      const data = await fetch(`${API_URL}/api/ai/status`).then(r => r.json());
      const val = data && typeof data === 'object' ? data : null;
      statusRef.current = val;
      setStatus(val);
    } catch (e) { /* silent */ }
  }, []);

  const loadHistory = useCallback(async () => {
    if (streamingRef.current) return;
    try {
      const data = await fetch(`${API_URL}/api/ai/chat/history?limit=100`).then(r => r.json());
      chatHistoryCache = data.messages || [];
      setMessages(chatHistoryCache);
    } catch (e) { /* silent */ }
  }, []);

  const loadProposals = useCallback(async () => {
    // Der Server entscheidet, ob überhaupt etwas zu bestätigen ist: bei
    // Autonomie "auto" liefert er eine leere Liste (die KI wendet ihre Wünsche
    // selbst an, sobald die Validierung sie freigibt). So können die Karten
    // nicht mehr kurz aufblitzen, bevor der Status geladen ist.
    try {
      const data = await fetch(`${API_URL}/api/ai/proposals/actionable?limit=20`)
        .then(r => r.json());
      setProposals(data.proposals || []);
    } catch (e) { /* silent */ }
  }, []);

  const loadInsights = useCallback(async () => {
    try {
      const data = await fetch(`${API_URL}/api/ai/insights`).then(r => r.json());
      setInsights(data && typeof data === 'object' ? data : null);
    } catch (e) { /* silent */ }
    try {
      const fg = await fetch(`${API_URL}/api/ai/fee-guard/stats?days=7`).then(r => r.json());
      setFeeGuardStats(fg && typeof fg === 'object' ? fg : null);
    } catch (e) { /* silent */ }
  }, []);

  // ---- Lektionen: Trader darf bearbeiten, löschen, ergänzen ----
  const approveSkippedWish = async (id) => {
    try {
      const res = await fetch(`${API_URL}/api/ai/lessons/skipped/approve`, {
        method: 'POST', headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify({ id }),
      });
      const d = await res.json();
      if (res.status === 401) { toast.error('Admin-Login erforderlich'); return; }
      if (!res.ok) throw new Error(d.detail || 'Bestätigen fehlgeschlagen');
      toast.success('Lektions-Wunsch bestätigt – Lektion ist jetzt aktiv');
      loadInsights();
    } catch (e) { toast.error(e.message); }
  };

  const deleteSkippedWish = async (id) => {
    try {
      const res = await fetch(`${API_URL}/api/ai/lessons/skipped/delete`, {
        method: 'POST', headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify({ id }),
      });
      const d = await res.json();
      if (res.status === 401) { toast.error('Admin-Login erforderlich'); return; }
      if (!res.ok) throw new Error(d.detail || 'Löschen fehlgeschlagen');
      toast.success('Lektions-Wunsch gelöscht');
      loadInsights();
    } catch (e) { toast.error(e.message); }
  };

  const saveLesson = async () => {
    if (!editLesson?.title?.trim()) { toast.error('Titel fehlt'); return; }
    try {
      const res = await fetch(`${API_URL}/api/ai/lessons/${editLesson.id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify({ title: editLesson.title, detail: editLesson.detail }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || 'Speichern fehlgeschlagen');
      toast.success('Lektion angepasst – für die KI ab jetzt unveränderlich');
      setEditLesson(null);
      loadInsights();
    } catch (e) { toast.error(e.message); }
  };

  const deleteLesson = async (id) => {
    try {
      const res = await fetch(`${API_URL}/api/ai/lessons/${id}`, {
        method: 'DELETE', headers: authHeaders(),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || 'Löschen fehlgeschlagen');
      toast.success('Lektion gelöscht');
      loadInsights();
    } catch (e) { toast.error(e.message); }
  };

  const createLesson = async () => {
    if (!newLesson?.title?.trim() || !newLesson?.detail?.trim()) {
      toast.error('Titel und Inhalt erforderlich'); return;
    }
    try {
      const res = await fetch(`${API_URL}/api/ai/lessons`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify(newLesson),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || 'Anlegen fehlgeschlagen');
      toast.success('Lektion angelegt – die KI kommentiert sie im Verlauf');
      setNewLesson(null);
      loadInsights();
    } catch (e) { toast.error(e.message); }
  };

  const loadRoles = useCallback(async () => {
    try {
      const data = await fetch(`${API_URL}/api/ai/roles`).then(r => r.json());
      setRoles(data.roles || null);
    } catch (e) { /* silent */ }
  }, []);

  useEffect(() => {
    loadStatus(); loadHistory(); loadProposals(); loadInsights(); loadRoles();
    const iv = setInterval(() => { loadStatus(); loadHistory(); loadProposals(); }, 12000);
    return () => clearInterval(iv);
  }, [loadStatus, loadHistory, loadProposals, loadInsights, loadRoles]);

  // Nur automatisch ans Ende scrollen, wenn der Nutzer ohnehin (fast) unten ist.
  // Scrollt der Nutzer nach oben, um zu lesen, bleibt die Position erhalten –
  // auch wenn das 12s-Polling neue Nachrichten nachlädt.
  const onChatScroll = () => {
    const el = chatAreaRef.current;
    if (!el) return;
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
    atBottomRef.current = atBottom;
    setShowJump(!atBottom);
  };

  const jumpToLatest = () => {
    atBottomRef.current = true;
    chatEndRef.current?.scrollIntoView({ behavior: 'smooth' });
    setShowJump(false);
  };

  useEffect(() => {
    if (atBottomRef.current) {
      chatEndRef.current?.scrollIntoView({ behavior: 'smooth' });
    }
  }, [messages, streamText]);

  // Beim Zurückwechseln in die Chat-Ansicht (z.B. aus dem KI-Labor) sofort
  // ans NEUESTE Ende des Verlaufs springen – nicht mehr oben beim ältesten.
  // Beim Öffnen des Chat-Fensters sofort ans Ende scrollen (Sprung, kein Smooth)
  const chatVisible = showChat;
  useEffect(() => {
    if (!chatVisible) return;
    atBottomRef.current = true;
    setShowJump(false);
    requestAnimationFrame(() => {
      const el = chatAreaRef.current;
      if (el) el.scrollTop = el.scrollHeight;
    });
  }, [chatVisible]);

  const cfg = status?.config || {};
  const decisions = status?.decisions || {};
  // Entscheidung (LONG/SHORT/HOLD) zu einem Coin finden – egal ob per Key oder Symbol abgelegt
  const decisionFor = (coin) => decisions[coin] || Object.values(decisions).find(d => d?.symbol === coin) || null;

  const updateConfig = async (updates) => {
    try {
      const res = await fetch(`${API_URL}/api/ai/config`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify(updates),
      });
      if (!res.ok) { toast.error('Nicht autorisiert'); return; }
      const data = await res.json();
      setStatus(prev => ({ ...prev, config: data.config }));
      if ('enabled' in updates) toast.success(`KI Trader ${updates.enabled ? 'AKTIVIERT' : 'gestoppt'}`);
    } catch (e) { toast.error('Verbindungsfehler'); }
  };

  const analyzeNow = async () => {
    setAnalyzing(true);
    try {
      const res = await fetch(`${API_URL}/api/ai/analyze`, { method: 'POST', headers: authHeaders() });
      const data = await res.json();
      if (data.status === 'ok') {
        toast.success(`Analyse fertig: ${data.decisions} Coins, ${(data.signals || []).length} Signal(e)`);
      } else {
        toast.error(data.detail || 'Analyse fehlgeschlagen');
      }
      loadStatus(); loadHistory();
    } catch (e) { toast.error('Verbindungsfehler'); }
    setAnalyzing(false);
  };

  const clearChat = async () => {
    await fetch(`${API_URL}/api/ai/chat`, { method: 'DELETE', headers: authHeaders() });
    chatHistoryCache = [];
    setMessages([]);
    toast.success('Chat geleert');
  };

  const learnNow = async () => {
    setLearning(true);
    try {
      const res = await fetch(`${API_URL}/api/ai/learn`, { method: 'POST', headers: authHeaders() });
      const data = await res.json();
      if (data.status === 'ok') {
        toast.success(`Lernlauf fertig: ${data.lessons} Lektionen gespeichert`
          + (data.new_lessons != null ? ` (${data.new_lessons} neu/geschärft)` : '')
          + (data.config_changes ? `, ${data.config_changes} Einstellungs-Änderung(en)` : ''));
      } else {
        toast.error(data.detail || 'Lernlauf fehlgeschlagen');
      }
      loadInsights(); loadHistory(); loadProposals(); loadStatus();
    } catch (e) { toast.error('Verbindungsfehler'); }
    setLearning(false);
  };

  const saveRole = async (roleKey, updates) => {
    try {
      const res = await fetch(`${API_URL}/api/ai/roles`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify({ [roleKey]: updates }),
      });
      if (!res.ok) { toast.error(res.status === 401 ? 'Admin-Login erforderlich' : 'Fehler'); return; }
      const data = await res.json();
      setRoles(data.roles || null);
    } catch (e) { toast.error('Verbindungsfehler'); }
  };

  const resetRole = async (roleKey) => {
    try {
      const res = await fetch(`${API_URL}/api/ai/roles/${roleKey}/reset`, {
        method: 'POST', headers: authHeaders(),
      });
      if (!res.ok) { toast.error(res.status === 401 ? 'Admin-Login erforderlich' : 'Fehler'); return; }
      const data = await res.json();
      setRoles(data.roles || null);
      toast.success('Voreinstellung wiederhergestellt');
    } catch (e) { toast.error('Verbindungsfehler'); }
  };

  const deepAnalyzeNow = async () => {
    setDeepRunning(true);
    try {
      const res = await fetch(`${API_URL}/api/ai/deep-analyze`, { method: 'POST', headers: authHeaders() });
      const data = await res.json();
      if (data.status === 'ok') toast.success(`Tiefenanalyse fertig (${data.model})`);
      else toast.error(data.detail || 'Tiefenanalyse fehlgeschlagen');
      loadHistory(); loadStatus();
    } catch (e) { toast.error('Verbindungsfehler'); }
    setDeepRunning(false);
  };

  const newsCheckNow = async () => {
    try {
      const res = await fetch(`${API_URL}/api/ai/news-check`, { method: 'POST', headers: authHeaders() });
      const data = await res.json();
      if (data.status === 'ok') {
        toast.success(data.alert ? `News-Alert (${data.severity}): ${data.summary?.slice(0, 80)}` : 'Keine relevanten News-Ereignisse');
      } else toast.error(data.detail || 'News-Check fehlgeschlagen');
      loadHistory();
    } catch (e) { toast.error('Verbindungsfehler'); }
  };

  const decideProposal = async (pid, action) => {
    try {
      const res = await fetch(`${API_URL}/api/ai/proposals/${pid}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify({ action }),
      });
      if (!res.ok) { toast.error(res.status === 401 ? 'Admin-Login erforderlich' : 'Fehler'); return; }
      toast.success(action === 'approve' ? 'Änderung übernommen' : 'Vorschlag abgelehnt');
      loadProposals(); loadHistory();
    } catch (e) { toast.error('Verbindungsfehler'); }
  };

  const sendMessage = async () => {
    const text = input.trim();
    if (!text || streaming) return;
    setInput('');
    setMessages(prev => [...prev, { id: `local-${Date.now()}`, role: 'user', text, ts: new Date().toISOString() }]);
    setStreaming(true);
    streamingRef.current = true;
    setStreamText('');
    let acc = '';
    try {
      const res = await fetch(`${API_URL}/api/ai/chat`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify({ message: text, coins: allSelected ? ['ALL'] : chatCoins }),
      });
      if (!res.ok) {
        toast.error(res.status === 401 ? 'Admin-Login erforderlich' : 'Chat-Fehler');
      } else {
        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buf = '';
        // eslint-disable-next-line no-constant-condition
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          buf += decoder.decode(value, { stream: true });
          let idx;
          while ((idx = buf.indexOf('\n\n')) >= 0) {
            const line = buf.slice(0, idx).trim();
            buf = buf.slice(idx + 2);
            if (!line.startsWith('data: ')) continue;
            try {
              const p = JSON.parse(line.slice(6));
              if (p.t) { acc += p.t; setStreamText(acc); }
              if (p.error) toast.error(p.error);
            } catch (e) { /* skip */ }
          }
        }
      }
    } catch (e) { toast.error('Verbindungsfehler'); }
    if (acc) {
      setMessages(prev => [...prev, { id: `local-a-${Date.now()}`, role: 'assistant', text: acc, ts: new Date().toISOString() }]);
    }
    setStreamText('');
    setStreaming(false);
    streamingRef.current = false;
  };

  const fmtTime = (ts) => {
    try { return new Date(ts).toLocaleTimeString('de-DE', { hour: '2-digit', minute: '2-digit', timeZone: 'Europe/Berlin' }); }
    catch { return ''; }
  };

  const renderMessage = (m) => {
    if (m.role === 'news_alert') {
      return (
        <div key={m.id} className="ai-msg ai-msg-news-alert" data-testid="ai-news-alert-message">
          <div className="ai-analysis-head">
            <Newspaper size={14} weight="fill" />
            <span>NEWS-WÄCHTER ALERT {m.severity ? `(${String(m.severity).toUpperCase()})` : ''}</span>
            <span className="ai-msg-time">{fmtTime(m.ts)}</span>
          </div>
          {m.text && <div className="ai-analysis-overview">{m.text}</div>}
          {(m.events || []).length > 0 && (
            <ul className="ai-news-event-list">
              {m.events.map((e, i) => (
                <li key={i}>
                  <b>{e.title}</b> · Impact {e.impact}
                  {(e.affects || []).length > 0 && <> · betrifft {(e.affects || []).map(coinLabel).join(', ')}</>}
                </li>
              ))}
            </ul>
          )}
        </div>
      );
    }
    if (m.role === 'deep_analysis') {
      return (
        <div key={m.id} className="ai-msg ai-msg-deep" data-testid="ai-deep-analysis-message">
          <div className="ai-analysis-head">
            <Brain size={14} weight="fill" />
            <span>TIEFENANALYSE {m.manual ? '(manuell)' : '(geplant)'}{m.weight_label ? ` · Gewicht ${m.weight_label}` : ''}</span>
            <span className="ai-msg-time">{fmtTime(m.ts)}</span>
          </div>
          {m.text && <div className="ai-analysis-overview">{m.text}</div>}
          {(m.outlook || []).length > 0 && (
            <div className="ai-analysis-decisions">
              {m.outlook.map((o, i) => (
                <div key={i} className={`ai-decision-row ${o.bias === 'bullish' ? 'ai-long' : o.bias === 'bearish' ? 'ai-short' : 'ai-hold'}`}>
                  <span className="ai-dec-sym">{coinLabel(o.symbol)}</span>
                  <span className="ai-dec-action">{(o.bias || 'neutral').toUpperCase()}</span>
                  <span className="ai-dec-reason">{o.key_levels ? `Levels: ${o.key_levels} · ` : ''}{o.szenario || ''}</span>
                </div>
              ))}
            </div>
          )}
          {(m.recommendations || []).length > 0 && (
            <ol className="ai-lesson-list">
              {m.recommendations.map((r, i) => <li key={i}>{r}</li>)}
            </ol>
          )}
        </div>
      );
    }
    if (m.role === 'learning') {
      return (
        <div key={m.id} className="ai-msg ai-msg-learning" data-testid="ai-learning-message">
          <div className="ai-analysis-head">
            <GraduationCap size={14} weight="fill" />
            <span>LERN-UPDATE {m.trigger === 'trade_close' ? '(nach Trade)' : m.trigger === 'daily' ? '(täglich)' : '(manuell)'}</span>
            <span className="ai-msg-time">{fmtTime(m.ts)}</span>
          </div>
          {m.text && <div className="ai-analysis-overview">{m.text}</div>}
          {(m.lessons || []).length > 0 && (
            <ol className="ai-lesson-list">
              {m.lessons.map((l, i) => <li key={i}><b>{l.title}</b>: {l.detail}</li>)}
            </ol>
          )}
        </div>
      );
    }
    if (m.role === 'config') {
      return (
        <div key={m.id} className="ai-msg ai-msg-config" data-testid="ai-config-message">
          <div className="ai-analysis-head">
            <Sliders size={14} weight="bold" />
            <span>EINSTELLUNGS-{(m.items || []).some(i => i.status === 'pending') ? 'VORSCHLAG' : 'ÄNDERUNG'}</span>
            <span className="ai-msg-time">{fmtTime(m.ts)}</span>
          </div>
          {m.text && <div className="ai-analysis-overview">{m.text}</div>}
          {(m.items || []).map((it, i) => (
            <div key={i} className={`ai-config-item ai-config-${it.status}`}>
              <b>{it.symbol === 'ENGINE' ? 'Engine' : coinLabel(it.symbol)}</b>{' '}
              {Object.entries(it.changes || {}).map(([k, v]) => (
                <span key={k} className="ai-prop-chg">{k}: <s>{String(it.current?.[k])}</s> → <b>{String(v)}</b></span>
              ))}
              <span className={`ai-config-status ai-config-status-${it.status}`}>
                {it.status === 'auto_applied' ? 'automatisch übernommen'
                  : it.status === 'applied' ? 'übernommen'
                    : it.status === 'rejected' ? 'abgelehnt' : 'wartet auf Bestätigung'}
              </span>
              {it.reason && <div className="ai-prop-reason">{it.reason}</div>}
            </div>
          ))}
        </div>
      );
    }
    if (m.role === 'summary') {
      const cfg = m.active_config || {};
      const counts = m.counts || {};
      const directives = Array.isArray(m.directives) ? m.directives : [];
      return (
        <div
          key={m.id}
          className={`ai-msg ai-msg-summary${m.pinned ? ' ai-msg-summary-pinned' : ''}`}
          data-testid="ai-summary-message"
        >
          <div className="ai-summary-head">
            <PushPin size={13} weight="fill" />
            <span className="ai-summary-badge" data-testid="ai-summary-badge">
              Tages-Zusammenfassung{m.day ? ` · ${m.day}` : ''}
            </span>
            {m.fallback && (
              <span className="ai-summary-fallback" title="LLM war nicht erreichbar – rein statistische Zusammenfassung">
                statistisch
              </span>
            )}
            <span className="ai-msg-time">{fmtTime(m.ts)}</span>
          </div>
          {m.text && <div className="ai-summary-text">{m.text}</div>}
          {(counts && Object.keys(counts).length > 0) && (
            <div className="ai-summary-metrics" data-testid="ai-summary-metrics">
              <span><b>{counts.analyses ?? 0}</b> Analysen</span>
              <span><b>{counts.signals ?? 0}</b> Signale</span>
              <span><b>{counts.long ?? 0}</b> LONG</span>
              <span><b>{counts.short ?? 0}</b> SHORT</span>
              <span><b>{counts.hold ?? 0}</b> HOLD</span>
            </div>
          )}
          {directives.length > 0 && (
            <div className="ai-summary-directives">
              <div className="ai-summary-sub">Deine Trader-Direktiven (aktuell aktiv):</div>
              <ul>
                {directives.slice(-6).map((d, i) => (<li key={i}>{d}</li>))}
              </ul>
            </div>
          )}
          {(cfg.provider || cfg.model) && (
            <div className="ai-summary-config" title="Aktive Konfiguration wonach die KI gerade tradet">
              KI tradet nach: <b>{cfg.provider}/{cfg.model}</b> · Intervall <b>{cfg.interval_min} min</b> · Min. Konfidenz <b>{cfg.min_confidence}%</b> · Cooldown <b>{cfg.cooldown_min} min</b> · News <b>{cfg.news_enabled ? 'an' : 'aus'}</b>
            </div>
          )}
        </div>
      );
    }
    if (m.role === 'analysis') {
      return (
        <div key={m.id} className="ai-msg ai-msg-analysis" data-testid="ai-analysis-message">
          <div className="ai-analysis-head">
            <Robot size={14} weight="fill" />
            <span>MARKT-ANALYSE {m.manual ? '(manuell)' : ''}</span>
            <span className="ai-msg-time">{fmtTime(m.ts)}</span>
          </div>
          {m.text && <div className="ai-analysis-overview">{m.text}</div>}
          {(m.decisions || []).length > 0 && (
            <div className="ai-analysis-decisions">
              {(m.decisions || []).map((d, i) => {
                const dk = `${m.id}-${i}`;
                const open = !!expandedDecs[dk];
                return (
                  <div key={i}
                    className={`ai-decision-row ${actionClass(d?.action)}${open ? ' expanded' : ''}`}
                    onClick={() => setExpandedDecs(p => ({ ...p, [dk]: !p[dk] }))}
                    title={open ? 'Einklappen' : 'Anklicken: komplette Begründung zu diesem Coin lesen'}
                    data-testid={`ai-decision-row-${d?.symbol || i}`}
                  >
                    <span className="ai-dec-sym">{coinLabel(d?.symbol)}</span>
                    <span className={`ai-dec-action ${actionClass(d?.action)}`}>{d?.action || '–'}</span>
                    {d?.horizon === 'swing' && <span className="ai-dec-swing">SWING</span>}
                    {String(d?.action).toUpperCase() === 'HOLD' ? (
                      <>
                        <span className="ai-dec-noedge" data-testid={`ai-dec-noedge-${d?.symbol || i}`}
                          title="HOLD = bewusst KEIN Trade. 'kein Edge' erscheint bei JEDER HOLD-Entscheidung – die KI sieht aktuell kein Setup mit klarem Vorteil (z.B. schlechtes Handelsfenster, Range ohne Level oder Trade-Sperre). Kein Fehler – der konkrete Grund steht rechts in der Begründung.">
                          <Prohibit size={11} weight="bold" /> kein Edge
                        </span>
                        {Number(d?.confidence) > 0 && (
                          <span className="ai-dec-conf" title="Konfidenz der HOLD-Entscheidung">{d.confidence}%</span>
                        )}
                      </>
                    ) : (
                      <span className="ai-dec-conf">{d?.confidence ?? 0}%</span>
                    )}
                    {d?.signaled && <span className="ai-dec-signaled" title="Signal ausgelöst"><Lightning size={11} weight="fill" /></span>}
                    <span className="ai-dec-reason">{d?.reasoning || ''}</span>
                    <span className="ai-dec-expand">{open ? <CaretUp size={11} /> : <CaretDown size={11} />}</span>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      );
    }
    const isUser = m.role === 'user';
    return (
      <div key={m.id} className={`ai-msg ${isUser ? 'ai-msg-user' : 'ai-msg-assistant'}`}
        data-testid={isUser ? 'ai-chat-user-message' : 'ai-chat-assistant-message'}>
        <div className="ai-msg-bubble">{m.text}</div>
        <div className="ai-msg-time">{fmtTime(m.ts)}</div>
      </div>
    );
  };

  const modelValue = (cfg.provider && cfg.model)
    ? `${cfg.provider}|${cfg.model}`
    : `${MODEL_OPTIONS[0].provider}|${MODEL_OPTIONS[0].model}`;

  return (
    <div className="ai-panel-overlay" onClick={onClose} data-testid="ai-trading-panel">
      <div className="ai-panel" onClick={e => e.stopPropagation()}>
        {/* Header */}
        <div className="ai-panel-header">
          <div className="ai-panel-title">
            <div className={`ai-robot-badge ${cfg.enabled ? 'on' : ''}`}><Robot size={20} weight="fill" /></div>
            <div>
              <h2>KI TRADER
                {((status?.providers_health?.rate_limited?.length || 0)
                  + (status?.providers_health?.errors?.length || 0)
                  + (status?.providers_health?.skipped_too_large?.length || 0)
                  + (status?.providers_health?.active_fallbacks?.length || 0)) > 0 && (
                  <span
                    className="ai-health-badge clickable"
                    data-testid="ai-health-badge"
                    onClick={() => setShowHealth(v => !v)}
                    title="Klicken für Details: welche KI-Modelle sind rate-limited, ausgefallen oder wegen zu großem Prompt übersprungen – und welche Assistenten gerade auf einem Fallback-Modell laufen"
                  >
                    <Warning size={13} weight="fill" />
                    {(status?.providers_health?.rate_limited?.length || 0)
                      + (status?.providers_health?.errors?.length || 0)
                      + (status?.providers_health?.skipped_too_large?.length || 0)
                      + (status?.providers_health?.active_fallbacks?.length || 0)}
                    {showHealth ? <CaretUp size={11} /> : <CaretDown size={11} />}
                  </span>
                )}
              </h2>
              {showHealth && (
                <div className="ai-health-dropdown" data-testid="ai-health-dropdown">
                  <div className="ai-health-dd-title">Modell-Status</div>
                  {(status?.providers_health?.rate_limited || []).map(m => (
                    <div className="ai-health-dd-row limited" key={`rl-${m.provider}/${m.model}`}>
                      <span className="ai-health-dd-model">{m.provider}/{m.model}</span>
                      <span>
                        Rate-Limit – frei in ca. {Math.ceil((m.cooldown_left_s || 0) / 60)} min
                        {m.role && ` · betroffen: ${roleLabelOf(m.role)}`}
                      </span>
                    </div>
                  ))}
                  {(status?.providers_health?.errors || []).map(m => (
                    <div className="ai-health-dd-row err" key={`er-${m.provider}/${m.model}`}>
                      <span className="ai-health-dd-model">{m.provider}/{m.model}</span>
                      <span>{m.detail || 'Fehler/Timeout'}{m.role && ` · betroffen: ${roleLabelOf(m.role)}`}</span>
                    </div>
                  ))}
                  {(status?.providers_health?.skipped_too_large || []).map(m => (
                    <div className="ai-health-dd-row skipped" key={`sk-${m.provider}/${m.model}`}
                      data-testid={`ai-skip-${m.provider}-${String(m.model).replace(/[^a-z0-9]/gi, '-')}`}>
                      <span className="ai-health-dd-model">{m.provider}/{m.model}</span>
                      <span>übersprungen – {m.detail || 'Prompt zu groß fürs Token-Budget'}</span>
                    </div>
                  ))}
                  {status?.providers_health?.fallback_active && (
                    <div className="ai-health-dd-row fallback">
                      Fallback aktiv → {status.providers_health.last_call?.provider}/{status.providers_health.last_call?.model}
                    </div>
                  )}
                  {(status?.providers_health?.active_fallbacks || []).length > 0 && (
                    <>
                      <div className="ai-health-dd-title" style={{ marginTop: 6 }}>
                        Aktive Fallbacks – diese Assistenten laufen gerade auf Ersatz-Modell
                      </div>
                      {(status.providers_health.active_fallbacks || []).map((f, i) => (
                        <div className="ai-health-dd-row fallback" key={`afb-${f.role}-${i}`}
                          data-testid={`ai-active-fallback-${f.role}`}>
                          <span className="ai-health-dd-model">{roleLabelOf(f.role)}</span>
                          <span>
                            arbeitet mit {f.provider}/{f.model}
                            {f.requested_model && f.requested_model !== f.model && ` (statt ${f.requested_model})`}
                            {f.key_index > 0 && ' · Backup-Key'}
                            {f.age_s != null && ` · seit ${Math.max(1, Math.round(f.age_s / 60))} min`}
                          </span>
                        </div>
                      ))}
                    </>
                  )}
                  {(status?.providers_health?.recent_failures || []).length > 0 && (
                    <>
                      <div className="ai-health-dd-title" style={{ marginTop: 6 }}>
                        Letzte Ausfälle – betroffener Assistent · Ursache · Fallback
                      </div>
                      {(status.providers_health.recent_failures || []).slice(0, 8).map((f, i) => (
                        <div className="ai-health-dd-row fail" key={`fl-${i}`} data-testid={`ai-fail-row-${i}`}>
                          <span className="ai-health-dd-model">{roleLabelOf(f.role)}</span>
                          <span>
                            {f.provider}/{f.model} · {FAIL_REASON[f.reason] || f.reason}
                            {f.detail && f.reason !== 'rate_limited' ? ` (${String(f.detail).slice(0, 90)})` : ''}
                            {f.age_s != null && ` · vor ${Math.max(1, Math.round(f.age_s / 60))} min`}
                            {f.fallback_used && ` · Fallback übernahm: ${f.fallback_used}`}
                          </span>
                        </div>
                      ))}
                    </>
                  )}
                  {((status?.providers_health?.rate_limited?.length || 0)
                    + (status?.providers_health?.errors?.length || 0)) === 0 && (
                    <div className="ai-health-dd-row">Alle Modelle wieder verfügbar.</div>
                  )}
                </div>
              )}
              <span className="ai-panel-sub">
                {cfg.enabled
                  ? `Aktiv · analysiert alle ${cfg.interval_min} min${status?.analyzing ? ' · analysiert gerade…' : ''}`
                  : 'Ausgeschaltet – aktiviere die KI, damit sie eigenständig analysiert & tradet'}
              </span>
            </div>
          </div>
          <div className="ai-panel-header-actions">
            <button
              className={`ai-toggle ${cfg.enabled ? 'on' : ''}`}
              onClick={() => updateConfig({ enabled: !cfg.enabled })}
              data-testid="ai-enable-toggle"
            >
              <span className="ai-toggle-knob" />
              <span className="ai-toggle-label">{cfg.enabled ? 'AN' : 'AUS'}</span>
            </button>
            <button className="ai-icon-btn" onClick={onClose} data-testid="ai-panel-close"><X size={18} /></button>
          </div>
        </div>

        {status?.enabled && !status?.has_key && (
          <div className="ai-warning" data-testid="ai-key-warning">
            ⚠ Für den Provider „{cfg.provider || 'gemini'}“ ist kein API-Key gesetzt (Render EnvVars:
            GEMINI_API_KEY / GROQ_API_KEY / OPENROUTER_API_KEY / MISTRAL_API_KEY /
            GITHUB_MODELS_TOKEN / CEREBRAS_API_KEY · Backup-Keys: z.B. OPENROUTER_API_KEY_BACKUP).
            Wähle im Setup ein Modell eines Providers, für den ein Key existiert.
          </div>
        )}
        {/* Limit-/Ausfall-/Fallback-Anzeige: welches Modell aktuell wirklich arbeitet */}
        {status?.enabled && ((status?.providers_health?.rate_limited?.length > 0)
          || (status?.providers_health?.errors?.length > 0)
          || status?.providers_health?.fallback_active) && (
          <div className="ai-limit-banner" data-testid="ai-limit-banner">
            <div className="ai-limit-head">
              {status.providers_health.fallback_active
                ? `Fallback aktiv: ${status.providers_health.last_call?.provider}/${status.providers_health.last_call?.model}`
                : (status.providers_health.rate_limited?.length > 0
                  ? 'Modell-Limit erreicht'
                  : 'Modell-Ausfall (Fehler/Timeout) – Fallback-Kette übernimmt')}
              {status.providers_health.last_call?.key_index > 0 && ' · Backup-Key'}
              {status.providers_health.last_call?.requested_model
                && status.providers_health.last_call.requested_model !== status.providers_health.last_call.model
                && ` (gewünscht: ${status.providers_health.last_call.requested_model})`}
            </div>
            {(status.providers_health.rate_limited || []).slice(0, 4).map(m => (
              <div className="ai-limit-row" key={`${m.provider}/${m.model}`}
                data-testid={`ai-limit-${m.provider}-${String(m.model).replace(/[^a-z0-9]/gi, '-')}`}>
                {m.provider}/{m.model}: Limit erreicht – frei in ca.{' '}
                {Math.ceil((m.cooldown_left_s || 0) / 60)} min
                {m.role && <> · betroffen: {roleLabelOf(m.role)}</>}
              </div>
            ))}
            {(status.providers_health.errors || []).slice(0, 2).map(m => (
              <div className="ai-limit-row err" key={`err-${m.provider}/${m.model}`}>
                {m.provider}/{m.model}: {m.detail}
                {m.role && <> · betroffen: {roleLabelOf(m.role)}</>}
              </div>
            ))}
            {(status.providers_health.skipped_too_large || []).slice(0, 3).map(m => (
              <div className="ai-limit-row skipped" key={`sk-${m.provider}/${m.model}`}>
                {m.provider}/{m.model}: übersprungen – {m.detail || 'Prompt zu groß'}
              </div>
            ))}
          </div>
        )}
        {status?.enabled && status?.last_error && (
          <div className="ai-warning" data-testid="ai-error-banner">
            ⚠ {friendlyAiError(status.last_error)}
            <div className="ai-error-raw">Technisch: {status.last_error}</div>
            {/FAILED_PRECONDITION|User location is not supported|location is not supported/i.test(status.last_error) && (
              <div style={{ marginTop: 6, fontSize: 12, opacity: 0.85 }}>
                Google blockiert deinen Server-Standort für den Gemini Free-Tier. Lösungen:
                <br />• Billing in <a href="https://aistudio.google.com/apikey" target="_blank" rel="noreferrer">AI Studio</a> aktivieren (Free-Tier-Preise bleiben) – hebt Regional-Sperre auf
                <br />• Render-Region auf US-West/Oregon umstellen
                <br />• Vertex AI (EU-Endpoint) statt AI Studio verwenden
              </div>
            )}
          </div>
        )}

        {/* Status row */}
        <div className="ai-status-row" {...statusDrag.props} data-testid="ai-status-row">
          <button className="ai-action-btn" onClick={analyzeNow} disabled={analyzing || status?.analyzing} data-testid="ai-analyze-now-btn">
            <ArrowsClockwise size={14} weight="bold" className={analyzing || status?.analyzing ? 'spin' : ''} />
            {analyzing || status?.analyzing ? 'Analysiert…' : 'Jetzt analysieren'}
          </button>
          <span className="ai-status-info">
            Letzte Analyse: <b>{status?.last_run ? fmtTime(status.last_run) : '—'}</b>
          </span>
          <button
            className={`ai-setup-toggle ${showLearn ? 'active' : ''}`}
            onClick={toggleLearn}
            data-testid="ai-learn-toggle"
          >
            <Brain size={12} weight="bold" /> Lernen{status?.learning?.lessons_count ? ` (${status.learning.lessons_count})` : ''}
          </button>
          <button
            className={`ai-setup-toggle ai-focus-toggle ${showChatFocus ? 'active' : ''}`}
            onClick={toggleChatFocus}
            title={`KI-Chat Fokus: ${allSelected ? 'alle Assets' : chatCoins.map(coinLabel).join(', ')}`}
            data-testid="ai-chat-focus-toggle"
          >
            <Coins size={12} weight="bold" />
            <span className="ai-focus-toggle-label">Asset-Fokus: {focusSummary}</span>
          </button>
          <button
            className={`ai-setup-toggle ${showTeam ? 'active' : ''}`}
            onClick={toggleTeam}
            data-testid="ai-team-toggle"
          >
            <Robot size={12} weight="bold" /> KI-Team
          </button>
          <button
            className={`ai-setup-toggle ${showLab ? 'active' : ''}`}
            onClick={toggleLab}
            title="Forschungs-Analyst, ML-Labor (Optuna/XGBoost), KI-Gedächtnis & Markt-Beobachter"
            data-testid="ai-lab-toggle"
          >
            <Flask size={12} weight="bold" /> KI-Labor
          </button>
          <button
            className={`ai-setup-toggle ${showStrat ? 'active' : ''}`}
            onClick={toggleStrat}
            title="Strategie-Labor: neue KI-Strategien im Ghost-Test & Freigabe"
            data-testid="ai-strategy-toggle"
          >
            <Flask size={12} weight="bold" /> Strategien
          </button>
          <button
            className={`ai-setup-toggle ${showGov ? 'active' : ''}`}
            onClick={toggleGov}
            title="MasterPrompt (oberstes Gebot) & Daten-Validierung der KI-Änderungen"
            data-testid="ai-master-toggle"
          >
            <Crown size={12} weight="bold" /> MasterPrompt
          </button>
          <button
            className={`ai-setup-toggle ${showChat ? 'active' : ''}`}
            onClick={toggleChat}
            title="KI-Chat öffnen/schließen – vollwertiges Chat-Fenster (Anweisungen & Fragen an die KI)"
            data-testid="ai-chat-toggle"
          >
            <ChatCircleDots size={12} weight="bold" /> Chat
          </button>
          <button className="ai-setup-toggle" onClick={toggleSetup} data-testid="ai-setup-toggle">
            Setup {showSetup ? <CaretUp size={12} /> : <CaretDown size={12} />}
          </button>
        </div>

        {/* Lern-Panel (collapsible) */}
        {showLearn && (
          <div className="ai-learn-panel" data-testid="ai-learn-panel">
            <div className="ai-learn-head">
              <span className="ai-learn-title">
                <Brain size={14} weight="fill" /> KI-Lernen · {(insights?.lessons || []).length} Lektionen
                {insights?.last_learn ? ` · zuletzt ${fmtTime(insights.last_learn)}` : ''}
              </span>
              <button className="ai-action-btn" onClick={learnNow} disabled={learning} data-testid="ai-learn-now-btn">
                <GraduationCap size={14} weight="bold" className={learning ? 'spin' : ''} />
                {learning ? 'Lernt…' : 'Jetzt lernen'}
              </button>
            </div>
            {insights?.stats?.totals && (
              <div className="ai-learn-stats" data-testid="ai-learn-stats">
                <span><b>{insights.stats.totals.signals}</b> Signale</span>
                <span>Winrate <b>{insights.stats.totals.signal_win_rate}%</b></span>
                <span>Paper-PnL <b>{(insights.stats.trades?.paper?.pnl ?? 0).toFixed(2)}</b> USDT</span>
                <span>Live-PnL <b>{(insights.stats.trades?.live?.pnl ?? 0).toFixed(2)}</b> USDT</span>
                <span><b>{insights.stats.totals.closed_trades ?? 0}</b> Trades geschlossen</span>
              </div>
            )}
            {insights?.assessment && <div className="ai-learn-assessment">{insights.assessment}</div>}
            {/* Belohnungssystem: Reward-Verlauf + Auswertung pro Markt-Regime */}
            <AIRewardPanel />
            {((insights?.skipped_items || []).length > 0 || (insights?.skipped || []).length > 0) && (
              <div className="ai-learn-empty" data-testid="ai-lesson-skipped">
                Zurückgestellte Lektions-Wünsche der KI (MasterPrompt / Validierung):
                <ul className="ai-lesson-list">
                  {(insights?.skipped_items || []).map((s) => (
                    <li key={s.id} className="ai-skip-item" data-testid={`ai-skip-item-${s.id}`}>
                      <span className="ai-skip-text">
                        <b>{s.title}</b>{s.reason ? `: ${s.reason}` : ''}
                        {s.ts && <i className="ai-skip-ts"> · {new Date(s.ts).toLocaleString('de-DE', { timeZone: 'Europe/Berlin' })}</i>}
                      </span>
                      <span className="ai-skip-actions">
                        {s.approvable !== false && (
                          <button className="ai-skip-btn ok" title="Bestätigen – wird sofort aktive, gesperrte Lektion"
                            onClick={() => approveSkippedWish(s.id)} data-testid={`ai-skip-approve-${s.id}`}>
                            <CheckCircle size={15} weight="bold" />
                          </button>
                        )}
                        <button className="ai-skip-btn del" title="Wunsch endgültig löschen"
                          onClick={() => deleteSkippedWish(s.id)} data-testid={`ai-skip-delete-${s.id}`}>
                          <Trash size={14} weight="bold" />
                        </button>
                      </span>
                    </li>
                  ))}
                  {(insights?.skipped_items || []).length === 0 &&
                    (insights?.skipped || []).map((s, i) => <li key={i}>{s}</li>)}
                </ul>
              </div>
            )}
            {(insights?.lesson_candidates || []).length > 0 && (
              <div className="ai-learn-empty" data-testid="ai-lesson-candidates">
                Lektions-Kandidaten – werden erst aktiv, wenn die KI dieselbe Erkenntnis
                mehrfach in den Trade-Daten wiedererkennt:
                <ul className="ai-lesson-list">
                  {insights.lesson_candidates.map((c, i) => (
                    <li key={c.key || i} data-testid={`ai-lesson-candidate-${i}`}>
                      <b>{c.title}</b>: {c.detail}{' '}
                      <i>({c.confirmations}× wiedererkannt · Datenbasis {c.sample} Ergebnisse)</i>
                    </li>
                  ))}
                </ul>
              </div>
            )}
            <div className="ai-learn-head" style={{ position: 'static', margin: '8px 0 4px', padding: 0, background: 'transparent', border: 'none' }}>
              <span className="ai-learn-title">Lektionen</span>
              <button className="ai-action-btn" onClick={() => setNewLesson({ title: '', detail: '', weight: 3 })}
                data-testid="ai-lesson-add-btn">
                <Plus size={13} weight="bold" /> Eigene Lektion
              </button>
            </div>
            {newLesson && (
              <div className="ai-lesson-edit" data-testid="ai-lesson-new-form">
                <input placeholder="Titel" value={newLesson.title}
                  onChange={e => setNewLesson({ ...newLesson, title: e.target.value })}
                  data-testid="ai-lesson-new-title" />
                <textarea rows={3} placeholder="Regel / Inhalt" value={newLesson.detail}
                  onChange={e => setNewLesson({ ...newLesson, detail: e.target.value })}
                  data-testid="ai-lesson-new-detail" />
                <div className="gov-actions">
                  <button className="gov-btn primary" onClick={createLesson} data-testid="ai-lesson-new-save">
                    <FloppyDisk size={13} weight="bold" /> Speichern
                  </button>
                  <button className="gov-btn" onClick={() => setNewLesson(null)} data-testid="ai-lesson-new-cancel">Abbrechen</button>
                </div>
              </div>
            )}
            {(insights?.lessons || []).length > 0 ? (
              <ol className="ai-lesson-list ai-lesson-list-plain" data-testid="ai-lesson-list">
                {insights.lessons.map((l, i) => (
                  <li key={l.id || i} data-testid={`ai-lesson-${l.id || i}`}>
                    {editLesson && l.id && editLesson.id === l.id ? (
                      <div className="ai-lesson-edit">
                        <input value={editLesson.title}
                          onChange={e => setEditLesson({ ...editLesson, title: e.target.value })}
                          data-testid="ai-lesson-edit-title" />
                        <textarea rows={3} value={editLesson.detail}
                          onChange={e => setEditLesson({ ...editLesson, detail: e.target.value })}
                          data-testid="ai-lesson-edit-detail" />
                        <div className="gov-actions">
                          <button className="gov-btn primary" onClick={saveLesson} data-testid="ai-lesson-edit-save">
                            <FloppyDisk size={13} weight="bold" /> Übernehmen
                          </button>
                          <button className="gov-btn" onClick={() => setEditLesson(null)} data-testid="ai-lesson-edit-cancel">Abbrechen</button>
                        </div>
                      </div>
                    ) : (
                      <div className="ai-lesson-row" style={l.superseded ? { opacity: 0.45 } : undefined}>
                        <div className="ai-lesson-body" style={l.superseded ? { textDecoration: 'line-through' } : undefined}>
                          {l.no != null && (
                            <span className="ai-lesson-no" data-testid={`ai-lesson-no-${l.id}`}
                              title="Nummer der Lektion – dieselbe Nummer, von der die KI spricht (z.B. „Lektion 6“)">
                              {l.no}
                            </span>
                          )}
                          <b>{l.title}</b>: {l.detail}
                          {l.locked && <span className="ai-lesson-locked" data-testid={`ai-lesson-locked-${l.id}`}>vom Trader</span>}
                          {l.superseded && (
                            <span className="ai-lesson-locked" style={{ background: 'rgba(255,120,60,0.18)' }}
                              title="Widerspruch zum gleichen Thema – die neueste Trader-Anweisung gilt, diese Lektion ist inaktiv (bleibt gespeichert)."
                              data-testid={`ai-lesson-superseded-${l.id}`}>ersetzt – neuere Anweisung gilt</span>
                          )}
                        </div>
                        <button className="ai-lesson-btn" title="Lektion bearbeiten"
                          onClick={() => setEditLesson({ id: l.id, title: l.title, detail: l.detail })}
                          data-testid={`ai-lesson-edit-${l.id}`}>
                          <PencilSimple size={14} weight="bold" />
                        </button>
                        <button className="ai-lesson-btn danger" title="Lektion löschen"
                          onClick={() => deleteLesson(l.id)}
                          data-testid={`ai-lesson-delete-${l.id}`}>
                          <Trash size={14} weight="bold" />
                        </button>
                      </div>
                    )}
                  </li>
                ))}
              </ol>
            ) : (
              <div className="ai-learn-empty">
                Noch keine Lektionen – die KI lernt automatisch aus geschlossenen Trades &amp; Signal-Ergebnissen
                (nach Trade-Close, täglich um Mitternacht und manuell über „Jetzt lernen“).
              </div>
            )}
          </div>
        )}

        {/* KI-Labor (Forschung, ML, Gedächtnis, Markt) */}
        {showLab && <AILabPanel />}

        {/* MasterPrompt & Daten-Validierung (nur Trader) */}
        {showGov && <AIGovernancePanel />}

        {/* Strategie-Labor: Ghost-Phase & Freigabe neuer KI-Strategien */}
        {showStrat && <AIStrategyLabPanel />}

        {/* Setup (collapsible) */}
        {showSetup && (
          <div className="ai-setup" data-testid="ai-setup-panel">
            <label>
              <span>KI-Modell</span>
              <select
                value={modelValue}
                onChange={e => {
                  const [provider, model] = e.target.value.split('|');
                  updateConfig({ provider, model });
                }}
                data-testid="ai-model-select"
              >
                {MODEL_OPTIONS.map(o => (
                  <option key={o.model} value={`${o.provider}|${o.model}`}>{o.label}</option>
                ))}
              </select>
            </label>
            <label>
              <span>Analyse-Intervall</span>
              <select value={cfg.interval_min || 10} onChange={e => updateConfig({ interval_min: Number(e.target.value) })} data-testid="ai-interval-select">
                {[5, 10, 15, 30, 60].map(v => <option key={v} value={v}>{`${v} min`}</option>)}
              </select>
            </label>
            <label>
              <span>Min. Konfidenz</span>
              <select value={cfg.min_confidence || 65} onChange={e => updateConfig({ min_confidence: Number(e.target.value) })} data-testid="ai-confidence-select">
                {[50, 60, 65, 70, 75, 80, 90].map(v => <option key={v} value={v}>{v}%</option>)}
              </select>
            </label>
            <label>
              <span>Trade-Cooldown</span>
              <select value={cfg.cooldown_min ?? 45} onChange={e => updateConfig({ cooldown_min: Number(e.target.value) })} data-testid="ai-cooldown-select">
                {[0, 15, 30, 45, 60, 120].map(v => <option key={v} value={v}>{v === 0 ? 'aus' : `${v} min`}</option>)}
              </select>
            </label>
            <label title="Wie viele KI-Trader-Trades dürfen pro Coin gleichzeitig offen sein (1–5). Nur der KI-Trader nutzt dieses Limit; andere Strategien bleiben bei 1 Trade pro Coin.">
              <span>Max. Trades pro Coin</span>
              <select value={cfg.max_trades_per_coin || 1}
                onChange={e => updateConfig({ max_trades_per_coin: Number(e.target.value) })}
                data-testid="ai-max-trades-select">
                {[1, 2, 3, 4, 5].map(v => <option key={v} value={v}>{v} Trade{v > 1 ? 's' : ''}</option>)}
              </select>
            </label>
            <label title="Diversifikations-Guard: max. gleichzeitig offene KI-Trades in DIESELBE Richtung (LONG bzw. SHORT). Verhindert Klumpen-Risiko durch viele gleichgerichtete Trades. 0 = aus. Hedges (Gegenrichtung) sind nie betroffen.">
              <span>Max. gleiche Richtung</span>
              <select value={cfg.max_same_direction ?? 3}
                onChange={e => updateConfig({ max_same_direction: Number(e.target.value) })}
                data-testid="ai-max-same-direction-select">
                {[0, 2, 3, 4, 5, 6, 8].map(v => <option key={v} value={v}>{v === 0 ? 'aus' : `${v} Trades`}</option>)}
              </select>
            </label>
            <label title="Cluster-Guard: Mindestabstand (%) zwischen Entries auf demselben Coin in dieselbe Richtung. Verhindert mehrere Einstiege in derselben Preiszone. 0 = aus.">
              <span>Min. Entry-Abstand</span>
              <input type="number" min={0} max={5} step={0.1}
                style={{ width: 60 }}
                key={`medp-${cfg.min_entry_distance_pct ?? 0.5}`}
                defaultValue={cfg.min_entry_distance_pct ?? 0.5}
                onBlur={e => {
                  const v = parseFloat(e.target.value) || 0;
                  if (v !== (cfg.min_entry_distance_pct ?? 0.5)) updateConfig({ min_entry_distance_pct: v });
                }}
                data-testid="ai-min-entry-distance-input" />
            </label>
            <label title="Korrelations-Guard: BTC/ETH/SOL zählen als EIN Richtungs-Risiko. Ein zweiter gleichgerichteter Trade auf einem anderen Coin dieser Gruppe wird blockiert, damit korrelierte Coins das Richtungs-Limit nicht umgehen.">
              <span>Korrelations-Guard</span>
              <select value={cfg.correlation_guard === false ? 'off' : 'on'}
                onChange={e => updateConfig({ correlation_guard: e.target.value === 'on' })}
                data-testid="ai-correlation-guard-select">
                <option value="on">an (BTC/ETH/SOL = 1 Risiko)</option>
                <option value="off">aus</option>
              </select>
            </label>
            <label title="Autonomie-Leitplanke: Spanne, in der die KI ihre Min. Konfidenz selbst ändern darf (Autonomie 'automatisch'). Außerhalb wird jede Änderung nur ein Vorschlag, den du bestätigen musst. Die Spanne selbst kann die KI nie ändern.">
              <span>KI-Spanne Konfidenz</span>
              <span style={{ display: 'inline-flex', gap: 4, alignItems: 'center' }}>
                <select value={cfg.tune_conf_min ?? 55} onChange={e => updateConfig({ tune_conf_min: Number(e.target.value) })} data-testid="ai-tune-conf-min-select">
                  {[35, 40, 45, 50, 55, 60, 65].map(v => <option key={v} value={v}>{v}%</option>)}
                </select>
                –
                <select value={cfg.tune_conf_max ?? 75} onChange={e => updateConfig({ tune_conf_max: Number(e.target.value) })} data-testid="ai-tune-conf-max-select">
                  {[65, 70, 75, 80, 85, 90].map(v => <option key={v} value={v}>{v}%</option>)}
                </select>
              </span>
            </label>
            <label title="Autonomie-Leitplanke: höchster Cooldown, den die KI selbst setzen darf. Höhere Werte werden nur Vorschlag.">
              <span>KI-Limit Cooldown</span>
              <select value={cfg.tune_cooldown_max ?? 45} onChange={e => updateConfig({ tune_cooldown_max: Number(e.target.value) })} data-testid="ai-tune-cooldown-max-select">
                {[15, 30, 45, 60, 120].map(v => <option key={v} value={v}>{`${v} min`}</option>)}
              </select>
            </label>
            <label title="Fee-Wächter: Ein KI-Trade wird nur eröffnet, wenn seine SL-Distanz mindestens das eingestellte Vielfache der Roundtrip-Fees (2× Gebühr, Standard 0,12%) beträgt. Blockt nur mathematisch garantierte Fee-Verlierer – die KI darf sonst frei scalpen. Gilt für alle KI-Trades inkl. Sammel-Trades.">
              <span>Fee-Wächter</span>
              <select value={cfg.fee_guard_enabled === false ? 'off' : 'on'} onChange={e => updateConfig({ fee_guard_enabled: e.target.value === 'on' })} data-testid="ai-fee-guard-enabled-select">
                <option value="on">an</option>
                <option value="off">aus</option>
              </select>
            </label>
            <label title="Mindest-SL-Distanz als Vielfaches der Roundtrip-Fees (0,12% bei 0,06% je Seite). Beispiel: 4× = 0,48% Mindest-SL-Distanz.">
              <span>Fee-Faktor (× Fees)</span>
              <select value={cfg.fee_guard_mult ?? 4} onChange={e => updateConfig({ fee_guard_mult: Number(e.target.value) })} data-testid="ai-fee-guard-mult-select">
                {[2, 3, 4, 5, 6, 8, 10].map(v => <option key={v} value={v}>{v}×</option>)}
              </select>
            </label>
            <label title="Blockier-Statistik: Wie oft der Fee-Wächter in den letzten 7 Tagen einen KI-Trade geblockt hat und welche Roundtrip-Gebühren (Kapital × Hebel × 2 × Fee) diese Trades mindestens gekostet hätten.">
              <span>Geblockt (7 Tage)</span>
              <span className="mono" data-testid="ai-fee-guard-stats">
                {feeGuardStats
                  ? `${feeGuardStats.blocked_total}×${feeGuardStats.blocked_collection ? ` (davon ${feeGuardStats.blocked_collection} Sammel)` : ''} · ~${(feeGuardStats.est_fees_saved_usdt ?? 0).toFixed(2)} $ Fees vermieden`
                  : '—'}
              </span>
            </label>
            <label title="Datensammel-Modus (Phase 4): Entscheidungen unter der Live-Schwelle (aber über der Sammel-Schwelle) werden als PAPER-Trades ausgeführt und mit data_collection=true markiert – nie live, kein Kapital, kein Telegram. Liefert dem ML-Training deutlich mehr gelabelte Trades.">
              <span>Datensammlung (Paper)</span>
              <select value={cfg.collection_enabled === false ? 'off' : 'on'} onChange={e => updateConfig({ collection_enabled: e.target.value === 'on' })} data-testid="ai-collection-enabled-select">
                <option value="on">an</option>
                <option value="off">aus</option>
              </select>
            </label>
            <label title="Sammel-Schwelle: Mindest-Konfidenz für Datensammel-Paper-Trades (unabhängig von der Live-Schwelle oben).">
              <span>Sammel-Konfidenz</span>
              <select value={cfg.collection_min_confidence ?? 60} onChange={e => updateConfig({ collection_min_confidence: Number(e.target.value) })} data-testid="ai-collection-conf-select">
                {[50, 55, 60, 65, 70].map(v => <option key={v} value={v}>{v}%</option>)}
              </select>
            </label>
            <label title="Eigener Cooldown pro Coin für Sammel-Trades (unabhängig vom Live-Cooldown).">
              <span>Sammel-Cooldown</span>
              <select value={cfg.collection_cooldown_min ?? 30} onChange={e => updateConfig({ collection_cooldown_min: Number(e.target.value) })} data-testid="ai-collection-cooldown-select">
                {[10, 15, 30, 45, 60].map(v => <option key={v} value={v}>{`${v} min`}</option>)}
              </select>
            </label>
            <label title="Max. gleichzeitig offene Sammel-Trades pro Coin (verbrauchen keine Live-Slots).">
              <span>Sammel-Trades/Coin</span>
              <select value={cfg.collection_max_per_coin ?? 2} onChange={e => updateConfig({ collection_max_per_coin: Number(e.target.value) })} data-testid="ai-collection-max-per-coin-select">
                {[1, 2, 3, 4, 5].map(v => <option key={v} value={v}>{v}</option>)}
              </select>
            </label>
            <label title="Max. Kapital (USDT Margin) pro KI-Trade. 0 = aus (Coin-Trade-Settings gelten). Wenn gesetzt, entscheidet die KI pro Trade selbst, wie viel Kapital (10-100% davon) sie einsetzt – nicht automatisch immer das Maximum.">
              <span><Coins size={13} /> Max. Kapital/Trade</span>
              <input type="number" min={0} step={1}
                style={{ width: 80 }}
                key={`mcpt-${cfg.max_capital_per_trade ?? 0}`}
                defaultValue={cfg.max_capital_per_trade ?? 0}
                onBlur={e => {
                  const v = parseFloat(e.target.value) || 0;
                  if (v !== (cfg.max_capital_per_trade ?? 0)) updateConfig({ max_capital_per_trade: v });
                }}
                data-testid="ai-max-capital-per-trade" />
            </label>
            <label title="Chance-Risiko-Verhältnis (TP1 zu SL): Spanne, in der sich die KI bei JEDEM Trade frei bewegen darf. Min wird technisch erzwungen; Max 0 = keine Obergrenze.">
              <span>CRV min / max</span>
              <span style={{ display: 'inline-flex', gap: 4, alignItems: 'center' }}>
                <input type="number" min={1} max={10} step={0.1}
                  style={{ width: 56 }}
                  key={`crvmin-${cfg.crv_min ?? 1.2}`}
                  defaultValue={cfg.crv_min ?? 1.2}
                  onBlur={e => {
                    const v = parseFloat(e.target.value) || 1.2;
                    if (v !== (cfg.crv_min ?? 1.2)) updateConfig({ crv_min: v });
                  }}
                  data-testid="ai-crv-min-input" />
                <span style={{ opacity: 0.6 }}>–</span>
                <input type="number" min={0} max={20} step={0.1}
                  style={{ width: 56 }}
                  key={`crvmax-${cfg.crv_max ?? 0}`}
                  defaultValue={cfg.crv_max ?? 0}
                  title="0 = keine Obergrenze"
                  onBlur={e => {
                    const v = parseFloat(e.target.value) || 0;
                    if (v !== (cfg.crv_max ?? 0)) updateConfig({ crv_max: v });
                  }}
                  data-testid="ai-crv-max-input" />
              </span>
            </label>
            <label title="Hebel-Modus für alle KI-Trades: Coin-Einstellungen = bisheriges Verhalten (Hebel aus den Coin-Trade-Settings) · Auto = die KI wählt pro Trade frei einen Hebel bis zum Max · Fest = immer derselbe Hebel. Swing-Trades bleiben zusätzlich auf den Swing-Max-Hebel gedeckelt.">
              <span>Hebel-Modus</span>
              <select value={cfg.lev_mode || 'coin'}
                onChange={e => updateConfig({ lev_mode: e.target.value })}
                data-testid="ai-lev-mode-select">
                <option value="coin">Coin-Einstellungen (Standard)</option>
                <option value="auto">Auto – KI wählt bis Max</option>
                <option value="fixed">Fester Hebel</option>
              </select>
            </label>
            {cfg.lev_mode === 'auto' && (
              <label title="Maximaler Hebel, den die KI im Auto-Modus pro Trade wählen darf (1-100).">
                <span>Auto-Hebel max.</span>
                <input type="number" min={1} max={100} step={1}
                  style={{ width: 70 }}
                  key={`levam-${cfg.lev_auto_max ?? 25}`}
                  defaultValue={cfg.lev_auto_max ?? 25}
                  onBlur={e => {
                    const v = parseInt(e.target.value, 10) || 25;
                    if (v !== (cfg.lev_auto_max ?? 25)) updateConfig({ lev_auto_max: v });
                  }}
                  data-testid="ai-lev-auto-max-input" />
              </label>
            )}
            {cfg.lev_mode === 'fixed' && (
              <label title="Fester Hebel für alle KI-Trades (1-100).">
                <span>Fester Hebel</span>
                <input type="number" min={1} max={100} step={1}
                  style={{ width: 70 }}
                  key={`levfx-${cfg.lev_fixed ?? 10}`}
                  defaultValue={cfg.lev_fixed ?? 10}
                  onBlur={e => {
                    const v = parseInt(e.target.value, 10) || 10;
                    if (v !== (cfg.lev_fixed ?? 10)) updateConfig({ lev_fixed: v });
                  }}
                  data-testid="ai-lev-fixed-input" />
              </label>
            )}
            <label className="ai-setup-check">
              <span><Newspaper size={13} /> News</span>
              <input type="checkbox" checked={cfg.news_enabled !== false}
                onChange={e => updateConfig({ news_enabled: e.target.checked })} data-testid="ai-news-toggle" />
            </label>
            <label title="Darf die KI ihre eigenen Trade-Einstellungen (SL, TP, Hebel, …) ändern? Der investierte Betrag ist IMMER gesperrt.">
              <span><Sliders size={13} /> Autonomie</span>
              <select value={cfg.autonomy || 'suggest'} onChange={e => updateConfig({ autonomy: e.target.value })} data-testid="ai-autonomy-select">
                {AUTONOMY_OPTIONS.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
              </select>
            </label>
            <label>
              <span>Lern-Zeitraum</span>
              <select value={cfg.learning_lookback_days || 14} onChange={e => updateConfig({ learning_lookback_days: Number(e.target.value) })} data-testid="ai-lookback-select">
                {[7, 14, 30, 60].map(v => <option key={v} value={v}>{v} Tage</option>)}
              </select>
            </label>
            <label>
              <span>Max. Lektionen</span>
              <select value={cfg.max_lessons || 10} onChange={e => updateConfig({ max_lessons: Number(e.target.value) })} data-testid="ai-max-lessons-select">
                {[5, 10, 15, 20, 25, 30, 40, 50, 60, 70, 80, 90, 100].map(v => <option key={v} value={v}>{v}</option>)}
              </select>
            </label>
            <label className="ai-setup-check" title="Selbst-Lernen aus Signal-/Trade-Ergebnissen">
              <span><GraduationCap size={13} /> Lernen</span>
              <input type="checkbox" checked={cfg.learning_enabled !== false}
                onChange={e => updateConfig({ learning_enabled: e.target.checked })} data-testid="ai-learning-toggle" />
            </label>
            <label className="ai-setup-check" title="Automatischer Lernlauf nach jedem geschlossenen KI-Trade (max. 1x pro 15 min)">
              <span>Lernen nach Trade</span>
              <input type="checkbox" checked={cfg.learn_on_trade_close !== false}
                onChange={e => updateConfig({ learn_on_trade_close: e.target.checked })} data-testid="ai-learn-on-close-toggle" />
            </label>
            <label className="ai-setup-check" title="SL/TP aus der KI-Analyse direkt für die Order verwenden (statt der Coin-Trade-Settings)">
              <span>KI-Levels für Orders</span>
              <input type="checkbox" checked={cfg.use_ai_levels === true}
                onChange={e => updateConfig({ use_ai_levels: e.target.checked })} data-testid="ai-levels-toggle" />
            </label>
            <label className="ai-setup-check" title="Krypto, Forex und Indizes/Rohstoffe in getrennten KI-Läufen analysieren – tiefere, asset-spezifischere Begründungen (3 LLM-Calls pro Zyklus)">
              <span>Gruppen-Analyse</span>
              <input type="checkbox" checked={cfg.group_analysis !== false}
                onChange={e => updateConfig({ group_analysis: e.target.checked })} data-testid="ai-group-analysis-toggle" />
            </label>
            <label className="ai-setup-check" title="Statische Info-Blöcke (Plattform-Wissen, Parameter der anderen Strategien) aus jedem Analyse-Prompt weglassen – spart Tokens/Kosten, Entscheidungsqualität bleibt gleich">
              <span>Lean-Prompt (spart Kosten)</span>
              <input type="checkbox" checked={cfg.lean_prompt !== false}
                onChange={e => updateConfig({ lean_prompt: e.target.checked })} data-testid="ai-lean-prompt-toggle" />
            </label>
            <label className="ai-setup-check" title="Geplante Analyse einer Gruppe überspringen, wenn sich der Markt seit dem letzten Lauf kaum bewegt hat, keine Position offen ist und zuletzt überall HOLD galt (max. 2 Skips in Folge) – spart LLM-Calls/Kosten">
              <span>Smart-Skip (spart Calls)</span>
              <input type="checkbox" checked={cfg.smart_skip !== false}
                onChange={e => updateConfig({ smart_skip: e.target.checked })} data-testid="ai-smart-skip-toggle" />
            </label>
            <label className="ai-setup-check" title="Übergeordnete Swing-Trades: eigene Kategorie mit niedrigem Hebel und weiten TP/SL, parallel zu kurzfristigen (auch gegenläufigen) Scalps auf demselben Asset">
              <span>Swing-Trades</span>
              <input type="checkbox" checked={cfg.swing_enabled !== false}
                onChange={e => updateConfig({ swing_enabled: e.target.checked })} data-testid="ai-swing-toggle" />
            </label>
            <label title="Hebel-Obergrenze für Swing-Trades (unabhängig vom Scalp-Hebel)">
              <span>Swing max. Hebel</span>
              <select value={cfg.swing_max_leverage || 8}
                onChange={e => updateConfig({ swing_max_leverage: Number(e.target.value) })}
                data-testid="ai-swing-maxlev-select">
                {[2, 3, 5, 8, 10, 15, 20].map(v => <option key={v} value={v}>{v}x</option>)}
              </select>
            </label>
            {/* Kosten-Dashboard: geschätzte Tokens pro KI-Rolle und Tag */}
            {tokenUsage?.days?.length > 0 && (              <div style={{ gridColumn: '1 / -1' }} data-testid="ai-token-usage">
                <div className="ai-learn-title" style={{ margin: '6px 0 4px' }}>
                  <Coins size={13} weight="fill" /> Token-Verbrauch (Schätzung, pro Tag &amp; Rolle)
                </div>
                {tokenUsage.days.slice(0, 5).map(d => (
                  <div key={d.date} style={{ fontSize: 12, opacity: 0.9, marginBottom: 2 }}
                    data-testid={`ai-token-day-${d.date}`}>
                    <b>{d.date.split('-').reverse().join('.')}</b>: {(d.tokens / 1000).toFixed(1)}k Tokens · {d.calls} Calls
                    {' — '}
                    {d.roles.slice(0, 4).map(r => `${r.role} ${(r.tokens / 1000).toFixed(1)}k`).join(' · ')}
                  </div>
                ))}
                <div style={{ fontSize: 11, opacity: 0.55 }}>{tokenUsage.note}</div>
              </div>
            )}
            <AITraderReset />
          </div>
        )}

        {/* KI-Team (Rollen: Modelle, Handelszeiten, Fallback-KI) */}
        {showTeam && (
          <div className="ai-team-panel" data-testid="ai-team-panel">
            <div className="ai-team-head">
              <span className="ai-team-title"><Robot size={14} weight="fill" /> KI-Team – Rollen &amp; Zusammenarbeit</span>
              <div className="ai-team-actions">
                <button className="ai-action-btn" onClick={deepAnalyzeNow} disabled={deepRunning} data-testid="ai-deep-analyze-btn">
                  <Brain size={13} weight="bold" className={deepRunning ? 'spin' : ''} />
                  {deepRunning ? 'Analysiert…' : 'Tiefenanalyse jetzt'}
                </button>
                <button className="ai-action-btn" onClick={newsCheckNow} data-testid="ai-news-check-btn">
                  <Newspaper size={13} weight="bold" /> News-Check jetzt
                </button>
              </div>
            </div>
            <div className="ai-team-hint">
              Jede Rolle kann ein eigenes Modell, aktive Handelszeiten (Berlin) und zwei Fallback-KIs
              (Fallback 1 &amp; 2) haben. Ohne eigene Auswahl erbt die Rolle das Haupt-Modell.
              Backup-Keys (z.B. OPENROUTER_API_KEY_BACKUP, CEREBRAS_API_KEY_BACKUP)
              greifen automatisch bei Rate-Limits. Lektionen &amp; Analysen stärkerer Modelle werden höher gewichtet.
            </div>
            {ROLE_DEFS.map(rd => {
              const rc = roles?.[rd.key] || {};
              const roleModelValue = rc.model ? `${rc.provider}|${rc.model}` : '';
              const fbValue = rc.fallback_model ? `${rc.fallback_provider}|${rc.fallback_model}` : '';
              const fb2Value = rc.fallback2_model ? `${rc.fallback2_provider}|${rc.fallback2_model}` : '';
              const hours = rc.active_hours;
              return (
                <div key={rd.key} className={`ai-role-card ${rc.enabled === false ? 'disabled' : ''}`} data-testid={`ai-role-card-${rd.key}`}>
                  <div className="ai-role-head">
                    <span className="ai-role-name">{rd.label}</span>
                    <span className="ai-role-desc">{rd.desc}</span>
                    {rc.user_configured === false && (
                      <span className="ai-role-preset" title="Empfohlene Voreinstellung – noch nicht von dir geändert">Voreinstellung</span>
                    )}
                    {rc.user_configured && (
                      <button className="ai-role-reset" onClick={() => resetRole(rd.key)}
                        title="Auf die empfohlene Voreinstellung zurücksetzen"
                        data-testid={`ai-role-reset-${rd.key}`}>
                        <ArrowCounterClockwise size={12} weight="bold" />
                      </button>
                    )}
                    <label className="ai-setup-check ai-role-enabled">
                      <input type="checkbox" checked={rc.enabled !== false}
                        onChange={e => saveRole(rd.key, { enabled: e.target.checked })}
                        data-testid={`ai-role-enabled-${rd.key}`} />
                      <span>aktiv</span>
                    </label>
                  </div>
                  <div className="ai-role-grid">
                    <label>
                      <span>Modell</span>
                      <select value={roleModelValue}
                        onChange={e => {
                          if (!e.target.value) { saveRole(rd.key, { provider: null, model: null }); return; }
                          const [provider, model] = e.target.value.split('|');
                          saveRole(rd.key, { provider, model });
                        }}
                        data-testid={`ai-role-model-${rd.key}`}>
                        <option value="">Haupt-Modell (erben)</option>
                        {MODEL_OPTIONS.map(o => (
                          <option key={o.model} value={`${o.provider}|${o.model}`}>{o.label}</option>
                        ))}
                      </select>
                    </label>
                    <label title="Außerhalb dieser Handelszeiten übernimmt automatisch die Fallback-KI">
                      <span>Handelszeiten (Berlin)</span>
                      <div className="ai-role-hours">
                        <label className="ai-role-always">
                          <input type="checkbox" checked={!hours}
                            onChange={e => saveRole(rd.key, {
                              active_hours: e.target.checked ? null : { start: '08:00', end: '22:00' },
                            })}
                            data-testid={`ai-role-always-${rd.key}`} />
                          24/7
                        </label>
                        {hours && (
                          <>
                            <input type="time" value={hours.start || '08:00'}
                              onChange={e => saveRole(rd.key, { active_hours: { ...hours, start: e.target.value } })}
                              data-testid={`ai-role-start-${rd.key}`} />
                            <span>–</span>
                            <input type="time" value={hours.end || '22:00'}
                              onChange={e => saveRole(rd.key, { active_hours: { ...hours, end: e.target.value } })}
                              data-testid={`ai-role-end-${rd.key}`} />
                          </>
                        )}
                      </div>
                    </label>
                    <label title="Springt ein, wenn das Rollen-Modell außerhalb der Handelszeiten ist oder komplett scheitert (auch bei leerer/unbrauchbarer Antwort)">
                      <span>Fallback 1</span>
                      <select value={fbValue}
                        onChange={e => {
                          if (!e.target.value) { saveRole(rd.key, { fallback_provider: null, fallback_model: null }); return; }
                          const [provider, model] = e.target.value.split('|');
                          saveRole(rd.key, { fallback_provider: provider, fallback_model: model });
                        }}
                        data-testid={`ai-role-fallback-${rd.key}`}>
                        <option value="">keine</option>
                        {MODEL_OPTIONS.map(o => (
                          <option key={o.model} value={`${o.provider}|${o.model}`}>{o.label}</option>
                        ))}
                      </select>
                    </label>
                    <label title="Zweite Fallback-Stufe: übernimmt, wenn Haupt-Modell UND Fallback 1 scheitern oder keine brauchbare Antwort liefern">
                      <span>Fallback 2</span>
                      <select value={fb2Value}
                        onChange={e => {
                          if (!e.target.value) { saveRole(rd.key, { fallback2_provider: null, fallback2_model: null }); return; }
                          const [provider, model] = e.target.value.split('|');
                          saveRole(rd.key, { fallback2_provider: provider, fallback2_model: model });
                        }}
                        data-testid={`ai-role-fallback2-${rd.key}`}>
                        <option value="">keine</option>
                        {MODEL_OPTIONS.map(o => (
                          <option key={o.model} value={`${o.provider}|${o.model}`}>{o.label}</option>
                        ))}
                      </select>
                    </label>
                    {rd.key === 'analyst' && (
                      <details className="ai-role-schedule" style={{ gridColumn: '1 / -1' }} data-testid="ai-analyst-schedule">
                        <summary style={{ cursor: 'pointer', fontSize: 12, opacity: 0.85 }}>⏱ Analyse-Zeitplan (Intervalle je Zeitfenster)</summary>
                        <AIScheduleEditor />
                      </details>
                    )}
                    {(rd.key === 'deep_analyst' || rd.key === 'research_analyst') && (
                      <label title="Uhrzeiten (Berlin), zu denen die Rolle täglich automatisch läuft">
                        <span>Geplante Zeiten</span>
                        <input type="text" className="ai-role-times"
                          defaultValue={(rc.schedule_times || []).join(', ')}
                          placeholder={rd.key === 'deep_analyst' ? '08:00, 20:00' : '06:30, 18:30'}
                          onBlur={e => {
                            const times = e.target.value.split(',').map(t => t.trim()).filter(Boolean);
                            saveRole(rd.key, { schedule_times: times });
                          }}
                          data-testid={`ai-role-times-${rd.key}`} />
                      </label>
                    )}
                    {rd.key === 'research_analyst' && (
                      <>
                        <label title="Spätestens nach dieser Zeit läuft eine neue Forschungs-Auswertung">
                          <span>Max. Abstand</span>
                          <select value={rc.interval_hours || 12}
                            onChange={e => saveRole('research_analyst', { interval_hours: Number(e.target.value) })}
                            data-testid="ai-role-research-interval">
                            {[4, 8, 12, 24, 48].map(v => <option key={v} value={v}>{v} h</option>)}
                          </select>
                        </label>
                        <label className="ai-setup-check" title="Automatisch auswerten, sobald neue Backtest-/Optimizer-/Regime-Lab-Ergebnisse fertig sind">
                          <span>Auto bei neuen Ergebnissen</span>
                          <input type="checkbox" checked={rc.auto_on_new_results !== false}
                            onChange={e => saveRole('research_analyst', { auto_on_new_results: e.target.checked })}
                            data-testid="ai-role-research-auto" />
                        </label>
                      </>
                    )}
                    {rd.key === 'market_observer' && (
                      <>
                        <label title="Wie oft der Marktzustand aller Coins gemessen und gespeichert wird">
                          <span>Scan-Intervall</span>
                          <select value={rc.interval_min || 15}
                            onChange={e => saveRole('market_observer', { interval_min: Number(e.target.value) })}
                            data-testid="ai-role-observer-interval">
                            {[5, 10, 15, 30, 60].map(v => <option key={v} value={v}>{`${v} min`}</option>)}
                          </select>
                        </label>
                        <label className="ai-setup-check" title="Zusätzlich eine kurze KI-Einschätzung des Marktzustands erzeugen (verbraucht LLM-Budget)">
                          <span>KI-Einschätzung</span>
                          <input type="checkbox" checked={rc.llm_summary === true}
                            onChange={e => saveRole('market_observer', { llm_summary: e.target.checked })}
                            data-testid="ai-role-observer-llm" />
                        </label>
                      </>
                    )}
                    {rd.key === 'news_watcher' && (
                      <>
                        <label>
                          <span>Check-Intervall</span>
                          <select value={rc.interval_min || 15}
                            onChange={e => saveRole('news_watcher', { interval_min: Number(e.target.value) })}
                            data-testid="ai-role-news-interval">
                            {[5, 10, 15, 30, 60].map(v => <option key={v} value={v}>{`${v} min`}</option>)}
                          </select>
                        </label>
                        <label className="ai-setup-check" title="Bei HIGH-Impact-Ereignissen sofort eine Analyse auslösen">
                          <span>Sofort-Analyse bei Alert</span>
                          <input type="checkbox" checked={rc.auto_analysis !== false}
                            onChange={e => saveRole('news_watcher', { auto_analysis: e.target.checked })}
                            data-testid="ai-role-news-autoanalysis" />
                        </label>
                      </>
                    )}
                  </div>
                </div>
              );
            })}
            <AITeamSupervisor
              roleLabels={ROLE_DEFS.reduce((acc, r) => ({ ...acc, [r.key]: r.label }), {})}
              onApplyModel={(roleKey, patch) => saveRole(roleKey, patch)}
            />
          </div>
        )}

        {/* Offene Einstellungs-Vorschläge der KI (nur in der Chat-Ansicht) */}
        {proposals.length > 0 && !showLab && !showGov && !showStrat && !showTeam && (
          <div className="ai-proposals-strip" data-testid="ai-proposals-strip">
            <div className="ai-proposals-title">
              <Sliders size={13} weight="bold" /> Einstellungs-Vorschläge der KI ({proposals.length})
            </div>
            <div className="ai-proposals-row" {...proposalDrag.props}
              data-testid="ai-proposals-row">
            {proposals.map(p => (
              <div key={p.id} className="ai-proposal-card" data-testid="ai-proposal-card">
                <div className="ai-prop-head">
                  <b className="ai-prop-sym">{p.symbol === 'ENGINE' ? 'Engine' : coinLabel(p.symbol)}</b>
                  <span className="ai-prop-changes">
                    {Object.entries(p.changes || {}).map(([k, v]) => (
                      <span key={k} className="ai-prop-chg">
                        {k}: <s>{String(p.current?.[k])}</s> → <b>{String(v)}</b>
                      </span>
                    ))}
                  </span>
                </div>
                {p.status && p.status !== 'pending' && (
                  <div className="ai-prop-wait" data-testid={`ai-proposal-status-${p.id}`}>
                    {p.status === 'needs_confirmation' ? 'wartet auf Bestätigungen' : 'wartet auf Daten'}
                    {': '}
                    {(p.macro_validation?.reason || p.validation?.reason || '')}
                    {p.clamped && ' · Schrittweite begrenzt'}
                  </div>
                )}
                {p.reason && <div className="ai-prop-reason">{p.reason}</div>}
                <div className="ai-prop-actions">
                  <button className="ai-prop-approve" onClick={() => decideProposal(p.id, 'approve')} data-testid={`ai-proposal-approve-${p.symbol}`}>
                    <CheckCircle size={14} weight="fill" /> Übernehmen
                  </button>
                  <button className="ai-prop-reject" onClick={() => decideProposal(p.id, 'reject')} data-testid={`ai-proposal-reject-${p.symbol}`}>
                    <XCircle size={14} weight="fill" /> Ablehnen
                  </button>
                </div>
              </div>
            ))}
            </div>
          </div>
        )}

        {/* Asset-Fokus – Auswahl der Assets für den Chat, nur bei geöffnetem Toggle sichtbar
            (Toggle sitzt in der Status-Row neben „Lernen"). Eingeklappt = 0px, verdeckt nichts. */}
        {showChatFocus && (
          <div className="ai-decisions-wrap" data-testid="ai-coin-selector">
            <div className="ai-chat-focus-bar">
              <span className="ai-coin-selector-title">
                KI-CHAT FOKUS
                <span className="ai-coin-selector-sep">·</span>
                <span className="ai-coin-selector-mode">
                  {allSelected ? 'ALLE ASSETS' : `AUSGEWÄHLT (${chatCoins.length})`}
                </span>
              </span>
              <div className="ai-focus-quick" data-testid="ai-focus-quick">
                <button
                  className={`ai-coin-all-toggle ${allSelected ? 'active' : ''}`}
                  onClick={() => persistCoins([...ALL_COINS])}
                  title="Alle Anlageklassen in den KI-Chat-Fokus nehmen"
                  data-testid="ai-focus-all-assets"
                >
                  Alle Assets
                </button>
                {/* Preset-Reiter je Anlageklasse (wie die Gruppen in der Sidebar) */}
                {ASSET_GROUPS.map(g => {
                  const gs = (g.symbols || []).map(x => x.symbol || x);
                  const isActive = !allSelected && gs.length > 0 &&
                    gs.every(s => chatCoins.includes(s)) && chatCoins.length === gs.length;
                  return (
                    <button key={g.name}
                      className={`ai-coin-all-toggle ${isActive ? 'active' : ''}`}
                      onClick={() => persistCoins(gs)}
                      title={`Nur ${g.name} in den KI-Chat-Fokus nehmen (${gs.length} Assets)`}
                      data-testid={`ai-focus-group-${g.name}`}
                    >
                      {g.name}
                    </button>
                  );
                })}
                <button
                  className="ai-coin-all-toggle"
                  onClick={() => persistCoins([selectedCoin])}
                  title="Nur das aktuell geöffnete Asset fokussieren"
                  data-testid="ai-focus-current"
                >
                  Nur {coinLabel(selectedCoin)}
                </button>
              </div>
            </div>
            <div
              className="ai-decisions-strip"
              data-testid="ai-decisions-strip"
              {...focusDrag.props}
            >
              {orderedCoins.map(coin => {
                const d = decisionFor(coin);
                const active = allSelected || chatCoins.includes(coin);
                const isCurrent = coin === selectedCoin;
                return (
                  <button
                    key={coin}
                    className={`ai-chip ${actionClass(d?.action)} ${active ? 'selected' : ''} ${isCurrent ? 'current' : ''}`}
                    onClick={() => toggleCoin(coin)}
                    title={d?.reasoning || (isCurrent ? 'Aktuell geöffneter Coin' : 'Anklicken, um den Coin für den KI-Chat auszuwählen')}
                    data-testid={`ai-coin-chip-${coin}`}
                  >
                    <span className="ai-chip-sym">{coinLabel(coin)}</span>
                    <span className="ai-chip-action">{d?.action || '–'}</span>
                    {d && <span className="ai-chip-conf">{d?.confidence ?? 0}%</span>}
                    {d?.signaled && <span className="ai-dec-signaled" title="Signal ausgelöst"><Lightning size={11} weight="fill" /></span>}
                  </button>
                );
              })}
            </div>
          </div>
        )}

        {/* Chat + Input – eigenes vollwertiges Fenster (wie MasterPrompt),
            über den „Chat"-Button in der Status-Row ein-/ausblendbar */}
        {showChat && (
        <>
        <div className="ai-chat-wrap">
        <div className="ai-chat-area" data-testid="ai-chat-area" ref={chatAreaRef} onScroll={onChatScroll}>
          {(() => {
            // Neueste angepinnte Summary ganz oben anzeigen, aus dem Haupt-Stream entfernen.
            // Dedupe (defensiv):
            //  (1) exakt-selbe id aus dem Stream entfernen
            //  (2) andere gepinnte Summaries desselben Tages entfernen
            //      – so bleibt garantiert genau eine sichtbare Tages-Zusammenfassung
            //        oben, selbst falls das Backend die Summary sowohl über die
            //        garantierte Pin-Rückgabe als auch im normalen Fenster liefert.
            const pinnedSummary = [...messages]
              .filter(m => m.role === 'summary' && m.pinned)
              .sort((a, b) => new Date(b.ts) - new Date(a.ts))[0];
            const pinnedId = pinnedSummary?.id;
            const pinnedDay = pinnedSummary?.day;
            const streamMessages = pinnedSummary
              ? messages.filter(m => {
                  if (m.id && m.id === pinnedId) return false;
                  if (m.role === 'summary' && m.pinned && m.day && m.day === pinnedDay) return false;
                  return true;
                })
              : messages;
            return (
              <>
                {pinnedSummary && (
                  <div className="ai-summary-pin-wrap" data-testid="ai-summary-pinned">
                    {renderMessage(pinnedSummary)}
                  </div>
                )}
                {streamMessages.length === 0 && !streaming && !pinnedSummary && (
                  <div className="ai-chat-empty">
                    <Robot size={36} weight="light" />
                    <p>Sag der KI, worauf sie achten soll – z.B.<br />
                      <em>„Achte auf den BTC-Support bei 60k"</em> oder <em>„Sei heute defensiv, nur Longs".</em><br />
                      Jede Nachricht fließt in die nächste Analyse ein.</p>
                  </div>
                )}
                {streamMessages.map(renderMessage)}
              </>
            );
          })()}
          {streaming && (
            <div className="ai-msg ai-msg-assistant">
              <div className="ai-msg-bubble">{streamText || <span className="ai-typing">KI denkt nach…</span>}</div>
            </div>
          )}
          <div ref={chatEndRef} />
        </div>
        {showJump && (
          <button
            className="ai-jump-latest"
            onClick={jumpToLatest}
            title="Zum neuesten Eintrag springen"
            data-testid="ai-jump-latest-btn"
          >
            <ArrowDown size={16} weight="bold" />
          </button>
        )}
        </div>

        {/* Input */}
        <AIQuickPrompts onPick={setInput} disabled={streaming} />
        <div className="ai-input-row">
          <input
            value={input}
            onChange={e => setInput(e.target.value)}
            onKeyDown={e => { if (e.key === 'Enter') sendMessage(); }}
            placeholder="Anweisung oder Frage an die KI…"
            disabled={streaming}
            data-testid="ai-chat-input"
          />
          <button className="ai-send-btn" onClick={sendMessage} disabled={streaming || !input.trim()} data-testid="ai-chat-send-btn">
            <PaperPlaneRight size={16} weight="fill" />
          </button>
          <button className="ai-icon-btn" onClick={clearChat} title="Chat leeren" data-testid="ai-chat-clear-btn">
            <Trash size={15} />
          </button>
        </div>
        </>
        )}
      </div>
    </div>
  );
};

export default AITradingPanel;
