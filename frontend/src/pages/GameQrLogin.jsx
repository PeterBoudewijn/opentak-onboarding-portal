import { useEffect, useState } from 'react';
import { useParams } from 'react-router-dom';
import { gameAPI } from '../services/api';
import { authStorage } from '../services/authStorage';
import './Auth.css';

function GameQrLogin() {
  const { team } = useParams();
  const [error, setError] = useState('');

  useEffect(() => {
    const secret = window.location.hash.replace(/^#/, '').trim();
    const normalizedTeam = String(team || '').toLowerCase();

    if (window.location.hash) {
      window.history.replaceState(null, '', window.location.pathname);
    }

    if (!['red', 'blue'].includes(normalizedTeam) || !secret) {
      setError('Ongeldige CO QR-code.');
      return;
    }

    gameAPI.qrLogin(normalizedTeam, secret)
      .then((response) => {
        authStorage.setTokens(response.data.access_token, response.data.refresh_token);
        authStorage.setUser(response.data.user);

        const base = (import.meta.env.BASE_URL || '/').replace(/\/$/, '');
        window.location.replace(`${base}${response.data.redirectPath}`);
      })
      .catch((err) => {
        setError(err.response?.data?.error || 'CO QR-login is mislukt.');
      });
  }, [team]);

  return (
    <div className="auth-container">
      <div className="auth-card">
        <h2>Airsoft Game</h2>
        {!error && <p>CO-login wordt geopend...</p>}
        {error && <div className="alert alert-error">{error}</div>}
      </div>
    </div>
  );
}

export default GameQrLogin;
