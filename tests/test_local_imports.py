import io
import pytest
from openpyxl import Workbook
from sqlalchemy import select
from app.local_imports import parse_rows, import_contacts, email_address, import_smtp_accounts
from app.models import Lead, Inbox
from app.local_classifier import classify_local_reply


def test_xlsx_russian_columns_and_date():
    workbook = Workbook()
    workbook.active.append(['Почта', 'Имя', 'Компания', 'ОКВЭД', 'Дата согласия'])
    workbook.active.append([' person@example.ru ', 'Иван', 'Компания', '62.01', '2026-10-20'])
    data = io.BytesIO(); workbook.save(data)
    rows = parse_rows(data.getvalue(), 'contacts.xlsx')
    assert rows[0]['email'] == 'person@example.ru'
    assert rows[0]['okved'] == '62.01'
    assert rows[0]['consent_date'] == '2026-10-20'


@pytest.mark.asyncio
async def test_import_deduplication_does_not_reactivate_unsubscribed(session):
    session.add(Lead(email='old@example.ru', status='unsubscribed'))
    await session.commit()
    result = await import_contacts(session, [
        {'email': 'new@example.ru', 'company': 'A'}, {'email': 'NEW@example.ru'},
        {'email': 'old@example.ru', 'consent_basis': 'Импорт'}, {'email': 'broken'},
    ])
    assert result['added'] == 1 and result['duplicates'] == 1 and result['invalid'] == 1
    assert (await session.scalar(select(Lead).where(Lead.email == 'old@example.ru'))).status == 'unsubscribed'


def test_syntax_and_idn_domain():
    assert email_address('user@пример.рф').endswith('@xn--e1afmkfd.xn--p1ai')
    for address in ['broken', '..x@example.com', 'a@local', 'x@bad..domain']:
        with pytest.raises(ValueError):
            email_address(address)


@pytest.mark.parametrize('reply, expected', [
    ('Не пишите мне больше', 'unsubscribed'), ('Не интересует, спасибо', 'not_interested'),
    ('Пришлите подробности', 'interested'), ('Я в отпуске', 'out_of_office'),
    ('Спасибо за письмо\n> Для отписки нажмите unsubscribe', None),
    ('Будем думать', None),
])
def test_local_reply_classification_preserves_uncertainty(reply, expected):
    assert classify_local_reply(reply) == expected


@pytest.mark.asyncio
async def test_account_import_never_echoes_password_and_uses_existing_tables(session, monkeypatch):
    monkeypatch.setenv('SMTP_ALLOW_PRIVATE_HOSTS', 'true')
    result = await import_smtp_accounts(session, [{'email': 'owner@example.ru', 'smtp_host': 'mail.example.com',
        'smtp_password': 'sensitive-test-password', 'smtp_port': 'not-a-number'}])
    assert result['saved'] == 0
    assert 'sensitive-test-password' not in str(result)
    assert (await session.execute(select(Inbox))).scalars().all() == []
