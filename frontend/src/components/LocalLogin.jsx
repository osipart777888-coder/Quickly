import { useState } from 'react';
import { useAuth } from '../context/AuthContext';

export default function LocalLogin({ firstUser }) {
  const { checkSetup, refreshToken } = useAuth();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  async function submit(event) {
    event.preventDefault(); setBusy(true); setError('');
    const fields = Object.fromEntries(new FormData(event.currentTarget));
    try {
      if (firstUser) {
        const registration = await fetch('/api/auth/register', {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(fields),
        });
        if (!registration.ok) throw await readError(registration);
        await checkSetup();
      }
      const response = await fetch('/api/auth/login', {
        method: 'POST', credentials: 'include', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username: fields.username, password: fields.password }),
      });
      if (!response.ok) throw await readError(response);
      if (!(await refreshToken())) throw new Error('Не удалось сохранить сеанс. Проверьте настройки cookie.');
    } catch (e) { setError(e.message); }
    finally { setBusy(false); }
  }
  return <form onSubmit={submit} className="space-y-3 mt-6">
    <label className="block text-sm">Логин<input name="username" required minLength={3} autoComplete="username" className="mt-1 block w-full rounded border-gray-300" /></label>
    {firstUser && <label className="block text-sm">Email администратора<input name="email" type="email" required autoComplete="email" className="mt-1 block w-full rounded border-gray-300" /></label>}
    <label className="block text-sm">Пароль<input name="password" type="password" required minLength={8} autoComplete={firstUser ? 'new-password' : 'current-password'} className="mt-1 block w-full rounded border-gray-300" /></label>
    {firstUser && <p className="text-xs text-gray-500">Минимум 8 символов: заглавная и строчная латинские буквы, цифра. Это локальная учётная запись Quickly.</p>}
    {error && <p role="alert" className="text-sm text-red-700">{error}</p>}
    <button disabled={busy} className="w-full rounded bg-blue-600 px-4 py-2 text-white disabled:opacity-50">{busy ? 'Подождите…' : firstUser ? 'Создать администратора' : 'Войти'}</button>
  </form>;
}
async function readError(response) {
  const data = await response.json();
  const detail = data.detail;
  return new Error(Array.isArray(detail) ? detail.map(item => item.msg).join('; ') : detail || 'Ошибка входа');
}
