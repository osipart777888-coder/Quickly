import {forwardRef,useCallback,useEffect,useRef} from 'react';
import ReactQuill from 'react-quill-new';
import DOMPurify from 'dompurify';
import 'react-quill-new/dist/quill.snow.css';
import {language,translate} from '../i18n';

export default forwardRef(function LocalizedQuill(props, forwardedRef){
  const wrapper=useRef(null);
  const lastChange=useRef(null);
  const ref=useCallback(value=>{if(typeof forwardedRef==='function')forwardedRef(value);else if(forwardedRef)forwardedRef.current=value;},[forwardedRef]);
  const onChange=useCallback((html,...args)=>{
    const safe=DOMPurify.sanitize(html);
    lastChange.current={html,safe};
    props.onChange?.(safe,...args);
  },[props.onChange]);
  useEffect(()=>{
    const labels={bold:'Bold',italic:'Italic',underline:'Underline',strike:'Strikethrough',list:'List',link:'Insert link',blockquote:'Block quote','code-block':'Code block',clean:'Clear formatting'};
    wrapper.current?.querySelectorAll('.ql-toolbar button').forEach(button=>{
      const key=Object.keys(labels).find(key=>button.classList.contains('ql-'+key));
      if(key){let text=translate(key==='list'?(button.value==='ordered'?'Numbered list':'Bullet list'):labels[key]);button.setAttribute('aria-label',text);button.setAttribute('title',text);}
    });
    wrapper.current?.querySelectorAll('.ql-header .ql-picker-label,.ql-header .ql-picker-item').forEach(item=>{
      const value=item.getAttribute('data-value');const text=value?(language==='ru'?'Заголовок ':'Heading ')+value:translate('Normal');item.setAttribute('data-label',text);item.setAttribute('aria-label',text);
    });
  });
  // Keep Quill's own HTML during editing: its serialization can differ from DOMPurify's.
  // Feeding the cleaned form back after every keystroke can recurse through onChange.
  const value=typeof props.value==='string'
    ? (lastChange.current?.safe===props.value?lastChange.current.html:DOMPurify.sanitize(props.value))
    : props.value;
  return <div ref={wrapper}><ReactQuill {...props} value={value} onChange={onChange} ref={ref}/></div>;
});
