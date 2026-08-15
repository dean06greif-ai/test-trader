import React, { useState } from 'react';
import { X, Lock } from '@phosphor-icons/react';
import { toast } from '../lib/toast';
import { setToken } from '../auth';
import './AdminLogin.css';

const API_URL = process.env.REACT_APP_BACKEND_URL;

const AdminLogin = ({ onClose, onSuccess }) => {
  const [username, setUsername] = useState('Admin');
  const [password, setPassword] = useState('');
  const [loading, setLoading] = useState(false);

  const submit = async (e) => {
    e.preventDefault();
    setLoading(true);
    try {
      const res = await fetch(`${API_URL}/api/auth/login`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username, password }),
      });
      if (res.ok) {
        const data = await res.json();
        setToken(data.token);
        toast.success('Admin angemeldet');
        onSuccess && onSuccess();
        onClose();
      } else {
        toast.error('Falsche Zugangsdaten');
      }
    } catch { toast.error('Verbindungsfehler'); }
    finally { setLoading(false); }
  };

  return (
    <div className="al-overlay" onClick={onClose}>
      <form className="al-panel" onClick={e => e.stopPropagation()} onSubmit={submit} data-testid="admin-login-modal">
        <div className="al-header">
          <div className="al-title"><Lock size={18} weight="fill" /> ADMIN-LOGIN</div>
          <button type="button" className="al-close" onClick={onClose} data-testid="admin-login-close"><X size={20} weight="bold" /></button>
        </div>
        <p className="al-hint">Nur der Admin kann Trades, Strategien & Einstellungen ändern.</p>
        <input className="al-input" placeholder="Benutzer" value={username} onChange={e => setUsername(e.target.value)} data-testid="admin-username" />
        <input className="al-input" type="password" placeholder="Passwort" value={password} onChange={e => setPassword(e.target.value)} data-testid="admin-password" autoFocus />
        <button className="al-submit" type="submit" disabled={loading} data-testid="admin-login-submit">
          {loading ? 'Anmelden...' : 'Anmelden'}
        </button>
      </form>
    </div>
  );
};

export default AdminLogin;
