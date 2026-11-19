"""Browser integration with in-page mocked APIs, no network navigation.
Run: python tests/hud/test_core_intent_browser.py (Playwright + Chromium required).
This validates the shipped JS DOM integration, not the real microphone/backend.
"""
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
PUBLIC = ROOT / 'jarvis/ui/web/frontend/public'

MOCK_FETCH = r"""
window.__fetchCalls=[];
window.fetch=async (url,options={})=>{
  window.__fetchCalls.push([String(url),options.method||'GET']);
  const responses={
    '/api/onboarding/state':{completed:true,tour_completed:true},
    '/api/hud/metrics':{cpu_percent:12,ram_percent:22,platform:'Test PC',tasks:2},
    '/api/voice/status':{ready:true},
    '/api/voice/state':{voice_state:'idle'},
    '/api/tasks?limit=8':{tasks:[{title:'Mathe lernen'},{title:'Netzwerkplan'}],total:2},
    '/api/tasks?limit=100':{tasks:[],total:2},
    '/api/wiki/index':{pages:[{title:'<img src=x onerror=alert(1)>'}]},
    '/api/health':{ok:true},
  };
  const data=responses[String(url)];
  return {ok:!!data,status:data?200:404,json:async()=>data||{}};
};
"""


def run():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path='/usr/bin/chromium', headless=True, args=['--no-sandbox'])
        page = browser.new_page(viewport={'width':1600,'height':900})
        errors=[]
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.goto('about:blank')
        page.set_content('<div id="root"><div data-testid="home-view"></div></div>')
        page.evaluate(MOCK_FETCH)
        page.add_script_tag(content=(PUBLIC/'jarvis-hud-intents.js').read_text())
        # The production iframe is same-origin; about:blank inherits the parent
        # origin here so Chromium can run the DOM bridge without a network stack.
        script=(PUBLIC/'jarvis-hud.js').read_text().replace(
            'src="/?view=chats&solo=1&jarvis_hud=off"','src="about:blank"'
        )
        page.add_script_tag(content=script)
        page.locator('#jarvis-chef-hud').wait_for(timeout=10000)
        page.locator('#jh-mode-switch').click()
        assert page.locator('#jarvis-chef-hud').evaluate("e=>e.classList.contains('jh-core-only')")
        frame=page.frames[1]
        frame.set_content('''<div data-testid="home-view"><div data-testid="voice-transcript" id="voice">
          <div data-testid="transcript-line" data-who="user"><span class="jarvis-user-bubble">Zeig mir meine alten Aufgaben</span></div>
          </div><div id="typed"></div></div>''')
        page.wait_for_timeout(2200)
        assert page.locator('#jh-core-popup').is_hidden(), 'historic chat replayed as new command'
        frame.evaluate('''() => {
          const line=document.createElement('div'); line.dataset.testid='transcript-line';
          line.dataset.who='user'; line.innerHTML='<span class="jarvis-user-bubble">Jarvis, zeig mir meine Aufgaben</span>';
          document.querySelector('#voice').appendChild(line);
        }''')
        page.wait_for_function("document.querySelector('#jh-popup-title')?.textContent==='MEINE AUFGABEN'",timeout=8000)
        page.get_by_text('Mathe lernen').wait_for()
        page.locator('#jh-popup-close').click()
        page.locator('[data-core-open="system"]').click()
        page.wait_for_function("document.querySelector('#jh-popup-title')?.textContent==='SYSTEMSTATUS'")
        frame.evaluate('''() => {
          const node=document.createElement('div');node.className='jarvis-user-bubble';
          node.textContent='Öffne den Chat';document.querySelector('#typed').appendChild(node);
        }''')
        page.wait_for_timeout(300)
        assert page.locator('#jh-popup-title').inner_text()=='SYSTEMSTATUS', 'automatic card overrode manual card'
        page.keyboard.press('Escape')
        frame.evaluate('''() => {
          const node=document.createElement('div');node.className='jarvis-user-bubble';
          node.textContent='Öffne das Gedächtnis';document.querySelector('#typed').appendChild(node);
        }''')
        page.wait_for_function("document.querySelector('#jh-popup-title')?.textContent==='GEDÄCHTNIS'",timeout=8000)
        page.get_by_text('<img src=x onerror=alert(1)>').wait_for()
        assert page.locator('#jh-popup-body img').count()==0, 'untrusted memory title became HTML'
        page.keyboard.press('Escape')
        frame.evaluate('window.__chatMarker=9281')
        page.locator('[data-core-open="chat"]').click()
        assert frame.evaluate('window.__chatMarker')==9281, 'iframe reloaded on popup'
        page.keyboard.press('Escape')
        assert frame.evaluate('window.__chatMarker')==9281, 'iframe reloaded on close'
        assert not errors, f'JS errors: {errors}'
        assert all(method=='GET' for _,method in page.evaluate('window.__fetchCalls')), 'intent caused a write API request'
        print('BROWSER INTEGRATION: PASS (final voice text, typed command, manual override, XSS-as-text, stable iframe)')
        browser.close()


if __name__=='__main__':
    run()
