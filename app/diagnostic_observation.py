"""Persisted observation policy, independent of transport acceptance."""
from datetime import timedelta,timezone
from email.utils import parsedate_to_datetime
from app.time import utcnow

DEFAULT_HOURS=48
DEFAULT_POLL_SECONDS=300

def policy(plan=None):
    return (getattr(plan,'observation_hours',None) or DEFAULT_HOURS,
            getattr(plan,'poll_interval_seconds',None) or DEFAULT_POLL_SECONDS)

def deadline_for(run,plan=None,*,reply=False):
    field='reply_observation_deadline' if reply else 'observation_deadline'
    deadline=getattr(run,field)
    if deadline is None:
        base=(run.reply_accepted_at if reply else run.accepted_at) or run.started_at or run.created_at
        deadline=base+timedelta(hours=policy(plan)[0])
        setattr(run,field,deadline)
    return deadline

def retry_delay(value,now=None):
    now=now or utcnow()
    if not value:return None
    try:
        seconds=float(value)
    except (TypeError,ValueError):
        try:seconds=(parsedate_to_datetime(str(value)).astimezone(timezone.utc).replace(tzinfo=None)-now).total_seconds()
        except (TypeError,ValueError,OverflowError):return None
    try:return timedelta(seconds=max(1,seconds))
    except (ValueError,OverflowError):return None

def folder_kind(folder,*,provider=''):
    names={x.strip().casefold().strip('"') for x in (folder or '').split(',')}
    if names & {'inbox','входящие','\\inbox'}:return 'inbox'
    if names & {'spam','junk','junk email','спам','\\junk','[gmail]/spam'}:return 'spam'
    if not names or names=={''} or (provider=='office365' and any('folder id:' in x for x in names)):
        return 'unknown'
    # Gmail custom IDs are labels, not proof of a mailbox folder.
    if provider=='gmail' and all(n.startswith('label_') for n in names):return 'unknown'
    return 'other'

def display_status(run):
    if run.status=='failed' and (run.report or {}).get('send_status')=='rejected':return 'rejected'
    if run.status in {'queued','created','dispatching','uncertain','failed','skipped','cancelled','reply_pending','reply_stopped'}:
        return run.status
    if run.observation_status=='expired':return 'expired'
    if run.observation_status=='temporarily_unavailable':return 'temporarily_unavailable'
    if run.status in {'reply_sent','reply_received'}:return run.status
    if run.received_at:return 'received_'+(run.folder_kind or 'unknown')
    return 'not_found' if run.observation_status=='not_found' else run.status


STATUS_RU = {'accepted': 'Принято SMTP/API-сервером', 'received_inbox': 'Обнаружено во «Входящих»', 'received_spam': 'Обнаружено в «Спаме»', 'received_other': 'Обнаружено в другой папке', 'received_unknown': 'Получение подтверждено; папка неизвестна', 'expired': 'Срок наблюдения истёк', 'reply_expired': 'Срок проверки ответа истёк', 'temporarily_unavailable': 'Проверка временно недоступна', 'not_found': 'Получение не обнаружено', 'rejected': 'Отправка отклонена сервером', 'uncertain': 'Результат отправки неизвестен', 'dispatching': 'Отправка выполняется', 'queued': 'Ожидает отправки'}
STATUS_RU.update({'created':'Создано','received':'Получение подтверждено','reply_sent':'Диагностический ответ принят сервером','reply_received':'Диагностический ответ получен','failed':'Ошибка','skipped':'Пропущено','cancelled':'Отменено','reply_pending':'Ответ ожидает отправки','reply_stopped':'Отправка ответа остановлена'})
