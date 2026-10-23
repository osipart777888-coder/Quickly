import { useCallback, useEffect, useMemo, useState } from 'react';
import { api } from '../api';
import EmailContent from '../components/EmailContent';
import { Button } from '../components/ui/Button';
import { Card } from '../components/ui/Card';


const ROLE_LABELS = {
  inbox: 'Входящие',
  sent: 'Отправленные',
  drafts: 'Черновики',
  spam: 'Спам',
  trash: 'Корзина',
  other: 'Папка',
};


function formatDate(value) {
  if (!value) return 'Дата неизвестна';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('ru-RU');
}


function errorText(error) {
  const raw = String(error?.message || error || 'Неизвестная ошибка');
  try {
    const parsed = JSON.parse(raw);
    return parsed?.detail || raw;
  } catch {
    return raw;
  }
}


export default function Mail() {
  const [accounts, setAccounts] = useState([]);
  const [accountFilter, setAccountFilter] = useState('');
  const [selectedInboxId, setSelectedInboxId] = useState('');
  const [folders, setFolders] = useState([]);
  const [selectedFolderId, setSelectedFolderId] = useState('');
  const [messages, setMessages] = useState([]);
  const [selectedMessage, setSelectedMessage] = useState(null);
  const [messageDetail, setMessageDetail] = useState(null);
  const [page, setPage] = useState(1);
  const [total, setTotal] = useState(0);
  const [totalPages, setTotalPages] = useState(1);
  const [searchInput, setSearchInput] = useState('');
  const [search, setSearch] = useState('');
  const [sync, setSync] = useState({ status: 'idle', error: '', last_refreshed_at: null });
  const [loadingAccounts, setLoadingAccounts] = useState(true);
  const [loadingFolders, setLoadingFolders] = useState(false);
  const [loadingMessages, setLoadingMessages] = useState(false);
  const [loadingDetail, setLoadingDetail] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState('');
  const [listNote, setListNote] = useState('');
  const [reloadToken, setReloadToken] = useState(0);

  const selectedAccount = useMemo(
    () => accounts.find(item => String(item.inbox_id) === String(selectedInboxId)),
    [accounts, selectedInboxId]
  );
  const selectedFolder = useMemo(
    () => folders.find(item => item.id === selectedFolderId),
    [folders, selectedFolderId]
  );
  const filteredAccounts = useMemo(() => {
    const needle = accountFilter.trim().toLowerCase();
    return needle
      ? accounts.filter(item => item.email.toLowerCase().includes(needle))
      : accounts;
  }, [accounts, accountFilter]);

  useEffect(() => {
    let active = true;
    setLoadingAccounts(true);
    api.get('/mail/accounts')
      .then(data => {
        if (!active) return;
        const rows = data.accounts || [];
        setAccounts(rows);
        const stored = localStorage.getItem('quicklyMailInboxId');
        const initial = rows.find(item => String(item.inbox_id) === stored && item.imap_configured)
          || rows.find(item => item.connection_test_ok && item.imap_configured)
          || rows.find(item => item.imap_configured);
        if (initial) setSelectedInboxId(String(initial.inbox_id));
      })
      .catch(err => active && setError(`Не удалось загрузить аккаунты: ${errorText(err)}`))
      .finally(() => active && setLoadingAccounts(false));
    return () => { active = false; };
  }, []);

  const loadFolders = useCallback(async ({ preserve = true } = {}) => {
    if (!selectedInboxId) return null;
    setLoadingFolders(true);
    setError('');
    try {
      const data = await api.get(`/mail/inboxes/${selectedInboxId}/folders`);
      const rows = data.folders || [];
      setFolders(rows);
      setSync(data.sync || {});
      setSelectedFolderId(current => {
        if (preserve && rows.some(item => item.id === current)) return current;
        return data.default_folder_id || rows.find(item => item.selectable)?.id || '';
      });
      return data;
    } catch (err) {
      setFolders([]);
      setSelectedFolderId('');
      setSync({ status: 'error', error: errorText(err), last_refreshed_at: new Date().toISOString() });
      setError(errorText(err));
      return null;
    } finally {
      setLoadingFolders(false);
    }
  }, [selectedInboxId]);

  useEffect(() => {
    if (!selectedInboxId) return;
    try { localStorage.setItem('quicklyMailInboxId', String(selectedInboxId)); } catch {}
    setFolders([]);
    setSelectedFolderId('');
    setMessages([]);
    setSelectedMessage(null);
    setMessageDetail(null);
    setPage(1);
    setSearchInput('');
    setSearch('');
    loadFolders({ preserve: false });
  }, [selectedInboxId, loadFolders]);

  useEffect(() => {
    if (!selectedInboxId || !selectedFolderId) return;
    let active = true;
    setLoadingMessages(true);
    setError('');
    setListNote('');
    const query = new URLSearchParams({
      folder_id: selectedFolderId,
      page: String(page),
      page_size: '50',
      search,
    });
    api.get(`/mail/inboxes/${selectedInboxId}/messages?${query.toString()}`)
      .then(data => {
        if (!active) return;
        setMessages(data.messages || []);
        setTotal(data.total || 0);
        setTotalPages(data.total_pages || 1);
        setSync(data.sync || {});
        if (data.search_limited) {
          setListNote('Сервер не поддержал поиск UTF-8: проверены последние 1000 писем.');
        } else if (data.sent_scan_limited) {
          setListNote('Показаны последние 20 000 серверных отправленных писем.');
        }
      })
      .catch(err => {
        if (!active) return;
        setMessages([]);
        setError(errorText(err));
        setSync({ status: 'error', error: errorText(err), last_refreshed_at: new Date().toISOString() });
      })
      .finally(() => active && setLoadingMessages(false));
    return () => { active = false; };
  }, [selectedInboxId, selectedFolderId, page, search, reloadToken]);

  const openMessage = useCallback(async (item) => {
    setSelectedMessage(item);
    setMessageDetail(null);
    setLoadingDetail(true);
    setError('');
    try {
      const query = new URLSearchParams({
        folder_id: selectedFolderId,
        source: item.source,
        message_key: item.message_key,
      });
      const data = await api.get(`/mail/inboxes/${selectedInboxId}/message?${query.toString()}`);
      setMessageDetail(data);
    } catch (err) {
      setError(errorText(err));
    } finally {
      setLoadingDetail(false);
    }
  }, [selectedInboxId, selectedFolderId]);

  const refresh = useCallback(async () => {
    if (!selectedInboxId) return;
    setRefreshing(true);
    setError('');
    try {
      const data = await api.post(`/mail/inboxes/${selectedInboxId}/refresh`, { folder_id: selectedFolderId || null });
      const rows = data.folders || [];
      setFolders(rows);
      setSync(data.sync || {});
      if (!rows.some(item => item.id === selectedFolderId)) {
        setSelectedFolderId(data.default_folder_id || rows.find(item => item.selectable)?.id || '');
        setPage(1);
      }
      setSelectedMessage(null);
      setMessageDetail(null);
      setReloadToken(value => value + 1);
    } catch (err) {
      setError(errorText(err));
    } finally {
      setRefreshing(false);
    }
  }, [selectedInboxId, selectedFolderId]);

  const submitSearch = (event) => {
    event.preventDefault();
    setPage(1);
    setSelectedMessage(null);
    setMessageDetail(null);
    setSearch(searchInput.trim());
  };

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-3 overflow-hidden p-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold">Почта</h1>
          <p className="text-sm text-gray-500">Просмотр серверных папок выбранного ящика в режиме чтения</p>
        </div>
        <div className="flex items-center gap-3 text-xs text-gray-500">
          <span className={`h-2.5 w-2.5 rounded-full ${sync.status === 'ok' ? 'bg-green-500' : sync.status === 'error' ? 'bg-red-500' : 'bg-gray-400'}`} />
          <span>
            {sync.status === 'error'
              ? sync.error || 'Ошибка синхронизации'
              : sync.last_refreshed_at
                ? `Обновлено ${formatDate(sync.last_refreshed_at)}`
                : 'Ожидание обновления'}
          </span>
          <Button onClick={refresh} disabled={!selectedInboxId || refreshing} variant="outline">
            {refreshing ? 'Обновление…' : 'Обновить'}
          </Button>
        </div>
      </div>

      {error && <div className="rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">{error}</div>}

      <div className="grid min-h-0 flex-1 grid-cols-1 gap-3 overflow-hidden xl:grid-cols-[250px_390px_minmax(0,1fr)]">
        <Card className="flex min-h-0 flex-col overflow-hidden p-3">
          <h2 className="mb-2 font-semibold">Почтовый ящик</h2>
          <input
            value={accountFilter}
            onChange={event => setAccountFilter(event.target.value)}
            placeholder="Найти аккаунт…"
            className="mb-2 w-full rounded border border-gray-300 px-3 py-2 text-sm"
          />
          <select
            value={selectedInboxId}
            onChange={event => setSelectedInboxId(event.target.value)}
            disabled={loadingAccounts}
            className="mb-4 w-full rounded border border-gray-300 px-2 py-2 text-sm"
          >
            {!selectedInboxId && <option value="">Выберите аккаунт</option>}
            {filteredAccounts.map(account => (
              <option key={account.inbox_id} value={account.inbox_id} disabled={!account.imap_configured}>
                {account.email}{account.imap_configured ? '' : ' — IMAP не настроен'}
              </option>
            ))}
          </select>

          <div className="mb-2 text-xs text-gray-500">
            {accounts.length ? `Подключено аккаунтов: ${accounts.length}` : 'Аккаунты не найдены'}
          </div>
          <h2 className="mb-2 mt-2 font-semibold">Папки</h2>
          <div className="min-h-0 flex-1 space-y-1 overflow-y-auto">
            {loadingFolders && <div className="text-sm text-gray-500">Получение папок…</div>}
            {!loadingFolders && folders.filter(folder => folder.selectable).map(folder => (
              <button
                key={folder.id}
                type="button"
                onClick={() => {
                  setSelectedFolderId(folder.id);
                  setPage(1);
                  setSelectedMessage(null);
                  setMessageDetail(null);
                }}
                className={`w-full rounded px-3 py-2 text-left text-sm ${selectedFolderId === folder.id ? 'bg-teal-100 font-semibold text-teal-900' : 'hover:bg-gray-100'}`}
              >
                <div>{ROLE_LABELS[folder.role] || folder.name}</div>
                {(folder.role === 'other' || folder.name !== ROLE_LABELS[folder.role]) && (
                  <div className="truncate text-xs font-normal text-gray-500">{folder.name}</div>
                )}
              </button>
            ))}
          </div>
        </Card>

        <Card className="flex min-h-0 flex-col overflow-hidden p-3">
          <div className="mb-3">
            <div className="mb-2 flex items-center justify-between gap-2">
              <h2 className="truncate font-semibold">{selectedFolder ? (ROLE_LABELS[selectedFolder.role] || selectedFolder.name) : 'Письма'}</h2>
              <span className="shrink-0 text-xs text-gray-500">{total} писем</span>
            </div>
            <form onSubmit={submitSearch} className="flex gap-2">
              <input
                value={searchInput}
                onChange={event => setSearchInput(event.target.value)}
                placeholder="Поиск в папке…"
                className="min-w-0 flex-1 rounded border border-gray-300 px-3 py-2 text-sm"
              />
              <Button type="submit" variant="outline">Найти</Button>
            </form>
            {search && (
              <button type="button" onClick={() => { setSearchInput(''); setSearch(''); setPage(1); }} className="mt-1 text-xs text-teal-700 hover:underline">
                Сбросить поиск «{search}»
              </button>
            )}
            {listNote && <div className="mt-2 text-xs text-amber-700">{listNote}</div>}
          </div>

          <div className="min-h-0 flex-1 space-y-2 overflow-y-auto pr-1">
            {loadingMessages && <div className="text-sm text-gray-500">Загрузка писем…</div>}
            {!loadingMessages && messages.length === 0 && selectedFolderId && (
              <div className="text-sm text-gray-500">В этой папке писем нет.</div>
            )}
            {messages.map(item => {
              const active = selectedMessage?.source === item.source && selectedMessage?.message_key === item.message_key;
              return (
                <button
                  key={`${item.source}:${item.message_key}`}
                  type="button"
                  onClick={() => openMessage(item)}
                  className={`w-full rounded border p-3 text-left ${active ? 'border-teal-400 bg-teal-50' : item.is_read ? 'border-gray-200 bg-white hover:bg-gray-50' : 'border-blue-200 bg-blue-50 hover:bg-blue-100/60'}`}
                >
                  <div className="flex items-start justify-between gap-2">
                    <span className={`truncate text-sm ${item.is_read ? 'font-medium' : 'font-bold'}`}>{item.subject || '(без темы)'}</span>
                    <span className="shrink-0 text-[11px] text-gray-400">{formatDate(item.date)}</span>
                  </div>
                  <div className="mt-1 truncate text-xs text-gray-500">От: {item.from || '—'}</div>
                  <div className="truncate text-xs text-gray-500">Кому: {item.to || '—'}</div>
                  <div className="mt-1 line-clamp-2 text-xs text-gray-600">{item.snippet || '(нет текстового фрагмента)'}</div>
                  {item.source === 'local' && <span className="mt-1 inline-block rounded bg-gray-100 px-1.5 py-0.5 text-[10px] text-gray-500">копия Quickly</span>}
                </button>
              );
            })}
          </div>

          <div className="mt-3 flex items-center justify-between border-t border-gray-200 pt-3">
            <Button variant="outline" disabled={page <= 1 || loadingMessages} onClick={() => setPage(value => Math.max(1, value - 1))}>Назад</Button>
            <span className="text-xs text-gray-500">Страница {page} из {totalPages}</span>
            <Button variant="outline" disabled={page >= totalPages || loadingMessages} onClick={() => setPage(value => value + 1)}>Далее</Button>
          </div>
        </Card>

        <Card className="flex min-h-0 flex-col overflow-hidden p-4">
          {!selectedMessage && !loadingDetail && (
            <div className="text-sm text-gray-500">Выберите письмо для просмотра.</div>
          )}
          {loadingDetail && <div className="text-sm text-gray-500">Загрузка полного письма…</div>}
          {messageDetail && !loadingDetail && (
            <>
              <div className="border-b border-gray-200 pb-3">
                <h2 className="text-lg font-semibold">{messageDetail.subject || '(без темы)'}</h2>
                <div className="mt-2 space-y-1 text-xs text-gray-600">
                  <div><strong>От:</strong> {messageDetail.from || '—'}</div>
                  <div><strong>Кому:</strong> {messageDetail.to || '—'}</div>
                  {messageDetail.cc && <div><strong>Копия:</strong> {messageDetail.cc}</div>}
                  <div><strong>Дата:</strong> {formatDate(messageDetail.date)}</div>
                </div>
              </div>
              <div className="mt-3 min-h-0 flex-1 overflow-auto">
                {messageDetail.body_html ? (
                  <>
                    <div className="mb-2 rounded bg-amber-50 px-3 py-2 text-xs text-amber-800">
                      Скрипты и автоматическая загрузка внешних изображений заблокированы.
                    </div>
                    <EmailContent html={messageDetail.body_html} stripTracking blockExternalResources />
                  </>
                ) : (
                  <pre className="whitespace-pre-wrap break-words font-sans text-sm">{messageDetail.body_plain || '(пустое письмо)'}</pre>
                )}
              </div>
            </>
          )}
        </Card>
      </div>
      {selectedAccount && <div className="text-xs text-gray-400">Выбран ящик: {selectedAccount.email}. Просмотр не меняет флаг прочтения на сервере.</div>}
    </div>
  );
}
