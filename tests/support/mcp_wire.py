"""Real MCP HTTP protocol against a loopback-only, synthetic server.

server: run with DATABASE_URL=.../quickly_r2_mcp_wire and BASE_URL=http://127.0.0.1:8000
client: run on the Mac against http://127.0.0.1:8767; no external mail services.
"""
import asyncio,json,os,sys
from contextlib import asynccontextmanager

async def server():
    from sqlalchemy.engine import make_url
    assert make_url(os.environ['DATABASE_URL']).database=='quickly_r2_mcp_wire'
    from app.main import app
    from app.database import init_db,AsyncSessionLocal
    from app.models import User,APIKey,Lead
    from app.auth import hash_api_key
    from app.time import utcnow
    from app.mcp_leads import leads_mcp_lifespan
    from datetime import timedelta
    from sqlalchemy import select,func
    import uvicorn
    @asynccontextmanager
    async def lifespan(_):
        await init_db()
        async with AsyncSessionLocal() as db:
            if not await db.scalar(select(func.count(User.id))):
                user=User(username='mcp-synthetic',email='mcp@local.test',role='admin',is_active=True)
                db.add(user);await db.flush()
                for name,scopes in [('read',['read']),('leads-read',['leads:read']),('leads-write',['leads:write']),('expired',['*']),('revoked',['*'])]:
                    db.add(APIKey(user_id=user.id,name=name,key_hash=hash_api_key('qk_mcp_synthetic_'+name),prefix='qk_mcp_test',scopes=scopes,revoked=name=='revoked',expires_at=utcnow()-timedelta(seconds=1) if name=='expired' else None))
                db.add(Lead(email='mcp-contact@local.test',name='Original'));await db.commit()
        async with leads_mcp_lifespan():yield
    app.router.lifespan_context=lifespan
    await uvicorn.Server(uvicorn.Config(app,host='0.0.0.0',port=8000,log_level='warning')).serve()

async def client():
    import httpx
    results=[];counter=0
    def decode(response):
        if 'text/event-stream' in response.headers.get('content-type',''):
            return json.loads(next(line[5:].strip() for line in response.text.splitlines() if line.startswith('data:')))
        return response.json()
    async with httpx.AsyncClient(base_url='http://127.0.0.1:8767',timeout=30) as http:
        for bearer in [False,True]:
            for scope in ['read','leads-read','leads-write','expired','revoked']:
                value='qk_mcp_synthetic_'+scope
                headers={'Authorization':'Bearer '+value} if bearer else {'X-API-Key':value}
                headers['Accept']='application/json, text/event-stream'
                async def call(method,params={}):
                    nonlocal counter
                    counter+=1
                    return await http.post('/api/mcp/',headers=headers,json={'jsonrpc':'2.0','id':counter,'method':method,'params':params})
                init=await call('initialize',{'protocolVersion':'2025-03-26','capabilities':{},'clientInfo':{'name':'synthetic-audit','version':'1'}})
                if scope in {'expired','revoked'}:
                    assert init.status_code==401
                    results.append({'case':scope,'bearer':bearer,'result':'passed'});continue
                assert init.status_code==200,init.status_code
                listed=decode(await call('tools/list'))
                names={x['name'] for x in listed['result']['tools']}
                assert {'list_leads','update_lead','delete_lead','add_campaign_leads'}<=names
                async def tool(name,arguments):
                    data=decode(await call('tools/call',{'name':name,'arguments':arguments}))
                    return json.loads(data['result']['content'][0]['text'])
                leads=await tool('list_leads',{})
                if scope=='leads-write':assert leads['status_code']==403
                else:assert isinstance(leads,list) and leads
                updated=await tool('update_lead',{'lead_id':1,'name':'Synthetic update'})
                if scope=='leads-write':assert not updated.get('error')
                else:
                    assert updated['status_code']==403
                    assert (await tool('delete_lead',{'lead_id':1}))['status_code']==403
                assert (await tool('add_campaign_leads',{'campaign_id':1,'leads':[{'email':'new@local.test'}]}))['status_code']==403
                assert (await tool('update_lead',{'lead_id':1,'enrollment_status':'active'}))['status_code']==403
                results.append({'case':scope,'bearer':bearer,'result':'passed'})
    print(json.dumps({'passed':len(results),'failed':0,'skipped':0,'transport':'real MCP Streamable HTTP over loopback TCP','cases':results}),flush=True)

asyncio.run(server() if sys.argv[1]=='server' else client())
