"""Visible semantic fields and read-only list/contact controls; no private APIs."""
import json


def snapshot(browser, source, name, error, initial=None):
    result = initial if initial is not None else browser.snapshot()
    extra = browser.execute_script(r"""(()=>{
        const visible=e=>!!e&&e.getClientRects().length>0&&getComputedStyle(e).visibility!=='hidden';
        const chrome=e=>!!e.closest('header,footer,nav,.header,.footer');
        const root=document.querySelector('main,[role=main]')||document.body;
        const labels=[];
        for(const e of root.querySelectorAll('tr,[role=row]')){
            if(!visible(e)||chrome(e))continue;
            const cells=Array.from(e.querySelectorAll('th,td,[role=cell],[role=rowheader]')).filter(visible);
            if(cells.length===2)labels.push({name:cells[0].innerText,value:cells[1].innerText});
        }
        for(const e of root.querySelectorAll('dt'))if(visible(e)&&e.nextElementSibling?.tagName==='DD')
            labels.push({name:e.innerText,value:e.nextElementSibling.innerText});
        const controls=[];let index=0;
        const rx=/^(показать (?:еще|ещё|следующие)(?: \d+)?|загрузить (?:еще|ещё)|следующ(?:ая|ая страница)|далее|next|next page|контакты|контактная информация|контактные данные|[›»→])$/i;
        for(const e of root.querySelectorAll('a,button,[role=button],[role=tab],div,span')){
            if(!visible(e)||chrome(e))continue;
            const text=(e.innerText||'').replace(/\s+/g,' ').trim();
            const label=e.getAttribute('aria-label')||e.getAttribute('title')||text;
            if(!rx.test(label)&&e.getAttribute('rel')!=='next')continue;
            if(Array.from(e.children).some(c=>visible(c)&&(c.innerText||'').replace(/\s+/g,' ').trim()===text))continue;
            const id=String(index++);e.setAttribute('data-ce-selection-control',id);
            controls.push({id,text,label,href:e.href||'',rel:e.getAttribute('rel')||'',
                disabled:!!e.disabled||e.getAttribute('aria-disabled')==='true'});
        }
        const passwords=Array.from(document.querySelectorAll('input[type=password]')).filter(visible);
        const login=passwords.some(e=>!chrome(e));
        const headings=Array.from(root.querySelectorAll('h1,h2,h3,[role=heading]')).filter(visible).filter(e=>!chrome(e)).map(e=>e.innerText);
        return {labels,controls,login_visible:login,login_offer:passwords.length>0,headings,
            empty:/ничего не найдено|результатов не найдено|найдено результатов:\s*0\b|не найдено организаций по вашим критериям/i.test(root.innerText)};
    })()""")
    if not isinstance(extra, dict):
        raise error(f"Не удалось прочитать интерфейс {name}")
    result[source] = extra
    return result


def click(browser, control):
    selector = '[data-ce-selection-control="' + str(control["id"]) + '"]'
    return browser.execute_script("(()=>{const e=document.querySelector(" + json.dumps(selector) + ");"
                                  "if(!e||e.disabled||e.getAttribute('aria-disabled')==='true')return false;e.click();return true})()")

