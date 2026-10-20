"""Conservative local reply labels; no external AI API or invented interest."""
import re


def classify_local_reply(text):
    lines = []
    for line in (text or '').splitlines():
        stripped = line.strip()
        if stripped.startswith('>') or re.match(r'(?i)(on .+ wrote:|[-_]{3,}.*(original|исходное)|.+писал\(а\):)', stripped):
            break
        lines.append(line)
    content = '\n'.join(lines).lower()[:8000]
    if re.search(r'не\s+(?:пишите|присылайте|отправляйте)|удалите\s+(?:мой\s+)?(?:адрес|email)|отпишите|отписаться|unsubscribe|remove me|do not (?:email|contact|write)|stop (?:emailing|sending)', content):
        return 'unsubscribed'
    if re.search(r'не\s*интерес(?:ует|но)|не\s+заинтересован|не\s+нужно|not interested|no thanks', content):
        return 'not_interested'
    if re.search(r'в\s+отпуске|отсутствую|out of office|on vacation|away from (?:the )?office', content):
        return 'out_of_office'
    if re.search(r'автоматическ(?:ий|ое)\s+(?:ответ|сообщение)|automatic reply|auto.?reply|automated response', content):
        return 'auto_reply'
    if re.search(r'ошиблись\s+адрес|не\s+тот\s+адрес|wrong person|no longer (?:work|with)', content):
        return 'wrong_person'
    if re.search(r'\bинтересует\b|пришлите\s+(?:подробности|информацию|предложение)|давайте\s+обсудим|готовы\s+обсудить|\bi am interested\b|\bwe are interested\b|send (?:me |us )?(?:more )?(?:details|information)', content):
        return 'interested'
    return None
