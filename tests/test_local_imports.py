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
@pytest.mark.parametrize('format', ['csv', 'xlsx'])
@pytest.mark.parametrize('header', ['email', 'Почта'])
async def test_email_only_contact_file(session, format, header):
    if format == 'xlsx':
        workbook = Workbook()
        workbook.active.append([header])
        workbook.active.append([' FIRST@example.ru '])
        workbook.active.append(['second@example.ru'])
        data = io.BytesIO()
        workbook.save(data)
        raw = data.getvalue()
    else:
        raw = ('\ufeff' + header + '\n FIRST@example.ru \nsecond@example.ru\n').encode()
    result = await import_contacts(session, parse_rows(raw, 'contacts.' + format))
    assert result == {'added': 2, 'updated': 0, 'duplicates': 0, 'invalid': 0, 'errors': []}
    leads = (await session.execute(select(Lead).order_by(Lead.email))).scalars().all()
    assert [lead.email for lead in leads] == ['first@example.ru', 'second@example.ru']
    assert all(lead.name == '' and lead.custom_data == {} for lead in leads)


@pytest.mark.asyncio
async def test_blank_optional_contact_fields_preserve_existing_details(session):
    session.add(Lead(email='existing@example.ru', name='Иван',
                     custom_data={'company': 'Компания', 'city': 'Москва'}, status='unsubscribed'))
    await session.commit()
    raw = ('email,name,company,city,okved,title,source,consent_date,consent_basis,segment\n'
           'existing@example.ru,,,,,,,,,\nnew@example.ru,,,,,,,,,\n').encode()
    result = await import_contacts(session, parse_rows(raw, 'contacts.csv'))
    assert result['added'] == 1 and result['updated'] == 1 and result['invalid'] == 0
    existing = await session.scalar(select(Lead).where(Lead.email == 'existing@example.ru'))
    assert existing.name == 'Иван'
    assert existing.custom_data == {'company': 'Компания', 'city': 'Москва'}
    assert existing.status == 'unsubscribed'
    added = await session.scalar(select(Lead).where(Lead.email == 'new@example.ru'))
    assert added.name == '' and added.custom_data == {}


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


@pytest.mark.asyncio
async def test_legacy_confirmation_columns_are_optional_metadata(session):
    result = await import_contacts(session, [{'email': 'legacy@example.ru', 'consent_date': 'not-a-date'}])
    assert result['added'] == 1 and result['invalid'] == 0


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
