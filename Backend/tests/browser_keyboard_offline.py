"""Offline Chrome DevTools pipe: real keyboard and 400px emulated viewport."""
import fcntl, json, os, select, shutil, socket, subprocess, tempfile, time
from pathlib import Path
from browser_workspace_offline import ROOT, FAKE

def main():
    for family in (socket.AF_INET,socket.AF_INET6):
        try: s=socket.socket(family);s.close()
        except PermissionError: continue
        raise SystemExit('Internet blocker is not active')
    chrome=shutil.which('google-chrome') or shutil.which('chromium')
    if not chrome: raise SystemExit('Chrome unavailable')
    public=ROOT/'Frontend/public';html=(public/'index.html').read_text();js=(public/'app.js').read_text();css=(public/'styles.css').read_text()
    html=html.replace('<link rel="stylesheet" href="/dashboard/styles.css"><script type="module" src="/dashboard/app.js"></script>',f'<style>{css}</style>',1)
    html=html.replace('</body>',FAKE+f'<script>{js}</script></body>')
    with tempfile.TemporaryDirectory(prefix='trendscout-cdp-') as d:
        page=Path(d)/'index.html';page.write_text(html)
        p2c_r,p2c_w=os.pipe();c2p_r,c2p_w=os.pipe()
        # Copy pipe endpoints above 10 so dup2 cannot overwrite the other endpoint.
        child_r=fcntl.fcntl(p2c_r,fcntl.F_DUPFD,10);child_w=fcntl.fcntl(c2p_w,fcntl.F_DUPFD,10)
        os.set_inheritable(child_r,True);os.set_inheritable(child_w,True)
        def child_setup(): os.dup2(child_r,3);os.dup2(child_w,4)
        env={k:v for k,v in os.environ.items() if k.lower() not in ('http_proxy','https_proxy','all_proxy','no_proxy')}
        cmd=[chrome,'--headless','--no-sandbox','--disable-gpu','--disable-background-networking','--disable-extensions','--no-proxy-server','--disable-sync','--disable-component-update','--no-first-run','--remote-debugging-pipe',f'--user-data-dir={d}/profile','about:blank']
        proc=subprocess.Popen(cmd,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,close_fds=False,preexec_fn=child_setup,env=env)
        for fd in (p2c_r,c2p_w,child_r,child_w): os.close(fd)
        pending=b'';counter=0
        def call(method,params=None,session=None):
            nonlocal pending,counter
            counter+=1;message={'id':counter,'method':method,'params':params or {}}
            if session: message['sessionId']=session
            os.write(p2c_w,json.dumps(message).encode()+b'\0'); deadline=time.monotonic()+15
            while time.monotonic()<deadline:
                while b'\0' in pending:
                    raw,pending=pending.split(b'\0',1)
                    if not raw: continue
                    reply=json.loads(raw)
                    if reply.get('id')==counter:
                        if 'error' in reply: raise RuntimeError(str(reply['error']))
                        return reply.get('result',{})
                ready,_,_=select.select([c2p_r],[],[],max(0,deadline-time.monotonic()))
                if not ready: break
                chunk=os.read(c2p_r,65536)
                if not chunk: break
                pending+=chunk
            raise RuntimeError('Chrome DevTools response timeout')
        try:
            target=call('Target.createTarget',{'url':'about:blank'})['targetId']
            session=call('Target.attachToTarget',{'targetId':target,'flatten':True})['sessionId']
            call('Page.enable',session=session)
            call('Emulation.setDeviceMetricsOverride',{'width':400,'height':800,'deviceScaleFactor':1,'mobile':True},session)
            call('Page.navigate',{'url':page.as_uri()},session)
            def evaluate(expr):
                result=call('Runtime.evaluate',{'expression':expr,'returnByValue':True},session)
                if 'exceptionDetails' in result: raise RuntimeError('evaluation failed')
                return result['result'].get('value')
            for _ in range(100):
                if evaluate("document.readyState==='complete' && !!document.querySelector('#workspace-nav')"): break
                time.sleep(.02)
            else: raise RuntimeError('page did not load')
            # Focus the first link and send a real Tab event through DevTools.
            evaluate("document.querySelector('[data-nav=overview]').focus()")
            for event in ('keyDown','keyUp'):
                call('Input.dispatchKeyEvent',{'type':event,'key':'Tab','code':'Tab','windowsVirtualKeyCode':9,'nativeVirtualKeyCode':9},session)
            if evaluate("document.activeElement.dataset.nav")!='sources': raise RuntimeError('Tab did not advance to Sources')
            # Navigate by keyboard using actual browser default handling.
            for event in ('keyDown','keyUp'):
                call('Input.dispatchKeyEvent',{'type':event,'key':'Enter','code':'Enter','windowsVirtualKeyCode':13,'nativeVirtualKeyCode':13},session)
            for _ in range(50):
                if evaluate("!document.querySelector('[data-view=sources]').hidden"): break
                time.sleep(.01)
            else: raise RuntimeError('Enter did not navigate')
            if evaluate('innerWidth')!=400: raise RuntimeError('viewport is not 400px')
            if evaluate('document.documentElement.scrollWidth > innerWidth'): raise RuntimeError('mobile overflow')
            # Explicitly open native dialog, then close with trusted Escape input.
            evaluate("document.querySelector('#detail-dialog').showModal()")
            if not evaluate("document.querySelector('#detail-dialog').open"): raise RuntimeError('dialog not opened')
            for event in ('keyDown','keyUp'):
                call('Input.dispatchKeyEvent',{'type':event,'key':'Escape','code':'Escape','windowsVirtualKeyCode':27,'nativeVirtualKeyCode':27},session)
            if evaluate("document.querySelector('#detail-dialog').open"): raise RuntimeError('Escape did not close dialog')
            call('Emulation.setDeviceMetricsOverride',{'width':320,'height':800,'deviceScaleFactor':1,'mobile':True},session)
            for name in ('overview','sources','runs','keywords','algorithms','quality','refresh','research','usage','settings'):
                evaluate('showView('+json.dumps(name)+')')
                if evaluate('document.documentElement.scrollWidth > innerWidth'): raise RuntimeError('320px overflow: '+name)
            print('Chrome DevTools pipe: 320/400px mobile, Tab, Enter, Escape PASS')
        finally:
            proc.terminate()
            try: proc.wait(timeout=5)
            except subprocess.TimeoutExpired: proc.kill();proc.wait()
            os.close(p2c_w);os.close(c2p_r)
if __name__=='__main__': main()
