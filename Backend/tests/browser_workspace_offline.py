"""Offline Chrome checks for the bilingual workspace with scoped API fixtures."""

import json
import os
import subprocess
import tempfile
from pathlib import Path

from browser_support import find_chrome, require_linux_network_isolation

ROOT = Path(__file__).resolve().parents[2]
PUBLIC = ROOT / "Frontend" / "public"
RUN_A = "a" * 32

FAKE = r"""<script>
window.testRequests=[];window.pwned=false;window.fixtureResearch=[];window.fixtureSchedules=[];
window.fetch=(path,opts={})=>{
  const request={path:String(path),method:opts.method||'GET',headers:opts.headers||{},body:opts.body||null};
  testRequests.push(request);
  const p=request.path;
  if(p==='/api/v1/collections'&&request.method==='POST')return Promise.resolve(new Response(JSON.stringify({item:{id:'d'.repeat(36),name:JSON.parse(request.body).name,created_by:'USER'}}),{status:201}));
  if(p==='/api/v1/collections/collection-a'&&request.method==='PATCH')return Promise.resolve(new Response(JSON.stringify({item:{id:'collection-a',name:JSON.parse(request.body).name||'Fixture Collection A'}}),{status:200}));
  if(p==='/api/v1/collections/collection-a'&&request.method==='DELETE')return Promise.resolve(new Response(JSON.stringify({deleted:true}),{status:200}));
  if(p==='/api/v1/collections/bulk'&&request.method==='POST')return Promise.resolve(new Response(JSON.stringify({updated:1,action:JSON.parse(request.body).action}),{status:200}));
  if(p==='/api/v1/research'&&request.method==='POST'){
    const item={...JSON.parse(request.body),id:'research-fixture',created_at:'2026-01-08T00:00:00Z',updated_at:'2026-01-08T00:00:00Z'};
    window.fixtureResearch=[item];
    return Promise.resolve(new Response(JSON.stringify({item}),{status:201}));
  }
  if(p==='/api/v1/research/research-fixture'&&request.method==='PATCH'){
    window.fixtureResearch=window.fixtureResearch.map(item=>({...item,...JSON.parse(request.body)}));
    return Promise.resolve(new Response(JSON.stringify({item:window.fixtureResearch[0]}),{status:200}));
  }
  if(p==='/api/v1/scheduler'&&request.method==='GET')return Promise.resolve(new Response(JSON.stringify({items:window.fixtureSchedules,implemented:true,enabled:false,worker_running:false}),{status:200}));
  if(p==='/api/v1/scheduler'&&request.method==='POST'){
    const item={...JSON.parse(request.body),id:'e'.repeat(32),enabled:false};window.fixtureSchedules=[item];
    return Promise.resolve(new Response(JSON.stringify({item}),{status:201}));
  }
  if(p==='/api/v1/scheduler/'+'e'.repeat(32)&&request.method==='PATCH'){
    window.fixtureSchedules=window.fixtureSchedules.map(item=>({...item,...JSON.parse(request.body)}));
    return Promise.resolve(new Response(JSON.stringify({item:window.fixtureSchedules[0]}),{status:200}));
  }
  if(p==='/api/timeout-json')return Promise.resolve({ok:true,json:()=>opts.signal.aborted?Promise.reject(new Error('aborted')):new Promise((resolve,reject)=>opts.signal.addEventListener('abort',()=>reject(new Error('aborted'))))});
  if(p==='/api/network-fail')return Promise.reject(new TypeError('offline network failure'));
  if(p==='/api/auth-fail')return Promise.resolve(new Response('{}',{status:401}));
  if(p==='/api/status-403')return Promise.resolve(new Response('{}',{status:403}));
  if(p==='/api/status-409')return Promise.resolve(new Response('{}',{status:409}));
  if(p==='/api/status-429')return Promise.resolve(new Response('{}',{status:429}));
  if(p==='/api/status-500')return Promise.resolve(new Response('{}',{status:500}));
  if(p==='/api/status-503')return Promise.resolve(new Response('{}',{status:503}));
  if(p==='/api/invalid-json')return Promise.resolve(new Response('{',{status:200}));
  if(p==='/api/v1/capabilities')return Promise.resolve(new Response(JSON.stringify({
    data_mode:'LIVE',local_live_demo:true,environment_mode:'local',read_only:false,
    fetch_enabled:false,public_fetch_enabled:false,authentication_required:false,
    sources:[
      {source_id:'wikimedia_pageviews_top_per_country',adapter_status:'IMPLEMENTED',approval_status:'PENDING_LIVE_APPROVAL',configuration_status:'NOT_REQUIRED',local_live_verification_status:'LOCAL_LIVE_VERIFIED',production_readiness:'NOT_PRODUCTION_READY',enabled_for_current_operation:false,last_saved_fetch:{status:'PARTIAL',record_count:7,fetched_at:'2026-11-14T01:00:00Z',data_mode:'SYNTHETIC'}},
      {source_id:'youtube_video_search',adapter_status:'IMPLEMENTED_OFFLINE_VERIFIED',approval_status:'PENDING_APPROVAL',configuration_status:'NOT_CONFIGURED',local_live_verification_status:'NOT_VERIFIED',production_readiness:'NOT_APPROVED',enabled_for_current_operation:false},
      {source_id:'google_web_serp',adapter_status:'IMPLEMENTED_OFFLINE_VERIFIED',approval_status:'PENDING_APPROVAL',configuration_status:'NOT_CONFIGURED',local_live_verification_status:'NOT_VERIFIED',production_readiness:'NOT_APPROVED',enabled_for_current_operation:false},
      {source_id:'bing_web_serp',adapter_status:'IMPLEMENTED_OFFLINE_VERIFIED',approval_status:'PENDING_APPROVAL',configuration_status:'NOT_CONFIGURED',local_live_verification_status:'NOT_VERIFIED',production_readiness:'NOT_APPROVED',enabled_for_current_operation:false}
    ]}),{status:200}));
  if(p==='/api/v1/sources/library')return Promise.resolve(new Response(JSON.stringify({audit_date:'2026-11-13',items:[
    {source_id:'youtube_video_search',display_name:'YouTube Search',category:'Video'},
    {source_id:'google_web_serp',display_name:'Google Search',category:'Search'},
    {source_id:'census_api',display_name:'US Census API',category:'Public data'}
  ]}),{status:200}));
  if(p==='/api/health')return Promise.resolve(new Response(JSON.stringify({status:'ok'}),{status:200}));
  if(p==='/api/runs/'+'d'.repeat(32))return Promise.resolve(new Response('{}',{status:404}));
  if(p.includes('/keywords/'))return Promise.resolve(new Response(JSON.stringify({
    keyword_id:'c'.repeat(64),display_keyword:'<img src=x onerror=window.pwned=true>',
    canonical_keyword:'safe-title',source:'Wikimedia',project:'en.wikipedia',
    observation_date:'2026-01-08',score:null,status:'INSUFFICIENT_DATA',
    current_views_ceil:0,baseline:null,ratio:null,observed_baseline_days:2,
    missing_baseline_dates:[{date:'2026-01-07',reason:'DAY_UNAVAILABLE'}],
    evidence_url:'javascript:alert(1)',source_url:'javascript:alert(1)',
    score_components:null,baseline_observations:[{date:'2026-01-06',views_ceil:0}],
    classification_evidence:[{category:'fashion',matched_text:'<script>',rule_id:'fixture'}],
    quality_flags:['<svg onload=window.pwned=true>']
  }),{status:200}));
  if(p==='/api/v1/collections')return Promise.resolve(new Response(JSON.stringify({
    mode:'AUTO_CREATE',items:[
      {id:'collection-a',name:'Fixture Collection A',description:'Parent Collection',examples:[],exclusions:[],membership_count:2,direct_membership_count:2,subtree_unique_keyword_count:2,created_by:'USER',auto_match:false},
      {id:'collection-b',name:'Fixture Collection B',description:'Merge destination',examples:[],exclusions:[],membership_count:1,direct_membership_count:1,subtree_unique_keyword_count:1,created_by:'USER',auto_match:false},
      {id:'collection-parent',name:'Parent with saved topics',description:'Parent with child records',examples:[],exclusions:[],membership_count:2,direct_membership_count:0,subtree_unique_keyword_count:2,created_by:'AI',auto_match:false},
      {id:'collection-child',name:'Saved child topics',description:'Child records',parent_id:'collection-parent',examples:[],exclusions:[],membership_count:2,direct_membership_count:2,subtree_unique_keyword_count:2,created_by:'AI',auto_match:false}
    ],suggestions:[],summary:{eligible_keywords:3,assigned_keywords:3,unclassified_keywords:0,excluded_keywords:0,classified_keywords:3,ambiguous_keywords:0,unknown_keywords:0,ineligible_topic_keywords:0,ai_assigned_keywords:0,manually_managed_keywords:3,ai_created_collections:0,sources:[]}
  }),{status:200}));
  if(p.startsWith('/api/v1/datasets/latest/keywords?')){
    const q=new URL(p,'https://local.invalid').searchParams;
    const empty=q.get('q')==='empty';
    return Promise.resolve(new Response(JSON.stringify({items:empty?[]:[{id:'c'.repeat(64),keyword:'Fixture keyword',source:'wikimedia_pageviews_top_per_country',observed_at:'2026-01-08',eligible:1,classification_status:'CLASSIFIED',memberships:[],evidence:{views_ceil:12}}],total:empty?0:51,next_offset:empty?null:25}),{status:200}));
  }
  if(p.startsWith('/api/v1/research?'))return Promise.resolve(new Response(JSON.stringify({items:window.fixtureResearch,total:window.fixtureResearch.length}),{status:200}));
  if(p.startsWith('/api/v1/runs/history?'))return Promise.resolve(new Response(JSON.stringify({items:[{
    fetch_id:'f'.repeat(32),run_at:'2026-11-14T01:00:00Z',run_type:'FETCH',
    sources:['wikimedia_pageviews_top_per_country'],status:'PARTIAL',keyword_count:10,
    received_record_count:12,retained_keyword_count:10,rejected_record_count:2,
    duration_seconds:null,mode:'synthetic',error_code:'SOURCE_COVERAGE_PARTIAL',quality_flags:['CENSORED_TOP_LIST']
  }],next_cursor:null,next_offset:null,manual_history_status:'COMPLETE'}),{status:200}));
  if(p==='/api/v1/runs/history/'+'f'.repeat(32))return Promise.resolve(new Response(JSON.stringify({item:{
    fetch_id:'f'.repeat(32),status:'PARTIAL',data_mode:'SYNTHETIC',
    source_manifest:[{source_id:'wikimedia_pageviews_top_per_country',status:'PARTIAL',record_count:10}],
    received_record_count:12,retained_record_count:10,rejected_record_count:2,
    error_code:'SOURCE_COVERAGE_PARTIAL',quality_flags:['CENSORED_TOP_LIST'],
    fetched_at:'2026-11-14T01:00:00Z'
  }}),{status:200}));
  if(p.includes('/keywords?')){
    const q=new URL(p,'https://local.invalid').searchParams;
    const empty=q.get('q')==='empty';
    const title=q.get('q')==='slow'?'OLD RESULT':q.get('q')==='fast'?'NEW RESULT':'<img src=x onerror=window.pwned=true>';
    return new Promise(resolve=>setTimeout(()=>resolve(new Response(JSON.stringify({
      items:empty?[]:[{keyword_id:'c'.repeat(64),display_keyword:title,project:'en.wikipedia',
        score:null,status:'INSUFFICIENT_DATA',current_views_ceil:0,baseline:null,
        ratio:null,observation_date:'2026-01-08',quality_flags:['<script>']}],
      total:empty?0:25,next_offset:q.get('offset')==='0'?20:null,
      empty_reason:empty?'NO_MATCH':null
    }),{status:200})),q.get('q')==='slow'?80:1));
  }
  if(p.startsWith('/api/runs/')){
    const id=p.split('/')[3];
    const payload={run_id:id,status:id==='b'.repeat(32)?'FETCHING':'PARTIAL',keyword_count:1234,scored_count:3,
      data_date:'2026-01-08',mode:'synthetic',fetched_at:'2026-01-09T00:00:00Z',
      coverage:[{date:'2026-01-07',status:'DAY_UNAVAILABLE',http_status:404}],
      algorithm_version:'wikimedia-trend-v0.1'};
    return new Promise(resolve=>setTimeout(()=>resolve(new Response(JSON.stringify(payload),{status:200})),id==='a'.repeat(32)?70:1));
  }
  return Promise.resolve(new Response('{}',{status:404}));
};
</script>"""

CHECK = r"""<script>
(async()=>{
  const errors=[];const check=(value,message)=>{if(!value)errors.push(message)};
  const wait=ms=>new Promise(resolve=>setTimeout(resolve,ms));
  try{
    await wait(300);
    if(RELOAD_TEST&&window.name!=='d2-reload'){
      document.querySelector('[data-theme-toggle]').click();
      document.querySelector('[data-locale-toggle]').click();
      await wait(120);
      window.name='d2-reload';location.reload();return;
    }
    if(window.name==='d2-reload'){
      check(document.documentElement.dataset.theme==='dark','theme persisted after reload');
      check(document.documentElement.lang==='en','locale persisted after reload');
      check(document.querySelector('[data-nav=overview]').textContent.includes('Overview'),'English navigation after reload');
      check(!document.querySelector('#operator-token'),'no operator token control');
      check(localStorage.getItem('trendscout.theme')==='dark','dark preference stored');
      check(localStorage.getItem('trendscout.locale')==='en','English preference stored');
      check(!Object.keys(localStorage).some(key=>/token|secret|credential/i.test(key)),'no credential in local storage');
      window.name='';
      document.querySelector('[data-theme-toggle]').click();
      document.querySelector('[data-locale-toggle]').click();
      await wait(120);
    }
    check(document.documentElement.dataset.theme==='light','light default');
    check(document.documentElement.lang==='vi','Vietnamese default');
    check(document.querySelectorAll('[data-nav]').length===8,'eight supported destination views remain available');
    check(document.querySelectorAll('#workspace-nav > .nav-link[data-nav]').length===5,'five primary navigation items');
    check(document.querySelector('.nav-advanced summary').textContent.includes('Nâng cao'),'technical navigation grouped under Advanced');
    check(document.querySelector('#open-fetch').disabled,'fetch disabled');
    check(!document.querySelector('#operator-token'),'no token input');
    check(!document.querySelector('#fetch-form'),'no fetch submit form');
    check(document.querySelectorAll('#source-registry-list .source-card').length===4,'four runtime source descriptors rendered from metadata');
    check(document.querySelectorAll('[data-dashboard-tab]').length===2,'two semantic dashboard tabs');
    check(document.querySelector('#metric-active-sources').textContent==='0','active source count comes from runtime capabilities');
    const wikimediaCard=[...document.querySelectorAll('#source-registry-list .source-card')].find(card=>card.querySelector('h2')?.textContent.includes('wikimedia_pageviews'));
    check(Boolean(wikimediaCard),'runtime metadata includes Wikimedia');
    check(wikimediaCard?.textContent.includes('\u0110\u00e3 x\u00e1c minh trong l\u1ea7n ch\u1ea1y LIVE local tr\u01b0\u1edbc \u0111\u00e2y'),'historical verification status is localized');
    check(wikimediaCard?.textContent.includes('Ch\u01b0a s\u1eb5n s\u00e0ng production'),'production readiness stays separate');
    check([...document.querySelectorAll('#source-registry-list .source-card')].filter(card=>!card.querySelector('h2')?.textContent.includes('wikimedia_pageviews')).every(card=>card.textContent.includes('Đã xác minh adapter ngoại tuyến')),'offline adapter states are reported from runtime metadata');
    const go=async name=>{
      if(innerWidth<=900 && !document.querySelector('#app-shell').classList.contains('menu-open'))document.querySelector('#menu-toggle').click();
      const link=document.querySelector('[data-nav="'+name+'"]');
      const group=link.closest('details');if(group)group.open=true;
      link.click();
      if(location.hash!=='#'+name)location.hash='#'+name;
      window.dispatchEvent(new Event('hashchange'));
      await wait(40);
    };
    for(const link of document.querySelectorAll('[data-nav]')){
      await go(link.dataset.nav);
      check(!document.querySelector('[data-view=\"'+link.dataset.nav+'\"]')?.hidden,'view '+link.dataset.nav);
      check(link.getAttribute('aria-current')==='page','aria current '+link.dataset.nav);
      check(document.documentElement.scrollWidth<=innerWidth+1,'horizontal overflow '+link.dataset.nav);
    }
    if(innerWidth<=900){
      const menuButton=document.querySelector('#menu-toggle');
      menuButton.click();
      check(document.querySelector('#app-shell').classList.contains('menu-open'),'mobile navigation opens');
      check(document.querySelector('#workspace-nav-panel').contains(document.activeElement),'mobile navigation receives focus');
      document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}));
      check(!document.querySelector('#app-shell').classList.contains('menu-open'),'Escape closes mobile navigation');
      check(document.activeElement===menuButton,'mobile navigation restores focus to its trigger');
    }
    await go('sources');await wait(40);
    const sourceLibrary=document.querySelector('#source-library-disclosure');
    check(Boolean(sourceLibrary&&!sourceLibrary.open),'secondary locked research inventory starts collapsed');
    const sourceSearch=document.querySelector('#source-library-search');sourceSearch.value='youtube';sourceSearch.dispatchEvent(new Event('input',{bubbles:true}));
    check(Boolean(sourceLibrary?.open&&document.querySelectorAll('#source-library-rows tr').length===1&&document.querySelector('#source-library-rows').textContent.includes('YouTube Search')),'searching the research inventory opens it and shows the matching source');
    sourceSearch.value='';sourceSearch.dispatchEvent(new Event('input',{bubbles:true}));
    check(document.querySelectorAll('#source-library-rows tr').length===3,'clearing research search restores all inventory candidates');
    const {applyLocale}=await import(I18N_MODULE_URI);
    await go('collections');await wait(80);
    if(innerWidth<=900){
      const filterToggle=document.querySelector('#workbench-filter-toggle');
      filterToggle.click();
      check(document.querySelector('#workbench-sidebar').classList.contains('is-open'),'mobile Collection filters open');
      check(document.querySelector('#workbench-sidebar').contains(document.activeElement),'opening Collection filters moves focus inside');
      document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}));
      check(!document.querySelector('#workbench-sidebar').classList.contains('is-open'),'Escape closes mobile Collection filters');
      check(document.activeElement===filterToggle,'closing Collection filters restores trigger focus');
    }
    const collapsedParent=document.querySelector('[data-collection-id="collection-parent"]');
    check(collapsedParent?.querySelector('.workbench-disclosure')?.getAttribute('aria-expanded')==='false','parent Collection starts collapsed in the fixture');
    check(collapsedParent?.querySelector('.workbench-tree-count')?.textContent==='2','collapsed parent shows unique keywords saved in its child Collections');
    const workbenchSearch=document.querySelector('#workbench-query');workbenchSearch.value='fixture';workbenchSearch.dispatchEvent(new Event('input',{bubbles:true}));await wait(420);
    check(testRequests.some(request=>request.path.startsWith('/api/v1/datasets/latest/keywords?')&&request.path.includes('q=fixture'))&&document.querySelector('#workbench-rows tr'),'Collection keyword search requests the entered query and renders its matching page');
    workbenchSearch.value='';workbenchSearch.dispatchEvent(new Event('input',{bubbles:true}));await wait(420);
    document.querySelector('#workbench-next').click();await wait(30);
    check(testRequests.some(request=>request.path.startsWith('/api/v1/datasets/latest/keywords?')&&request.path.includes('offset=25'))&&document.querySelector('#workbench-page-info').textContent.includes('26'),'Collection keyword pagination loads the following page');
    document.querySelector('#workbench-prev').click();await wait(30);
    document.querySelector('.workbench-context-button')?.click();
    const picker=document.querySelector('.collection-picker');
    const pickerTrigger=picker?.querySelector('[role=combobox]');
    pickerTrigger?.click();
    picker?.querySelector('[role=option]')?.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true}));
    check(pickerTrigger?.textContent.includes('Fixture Collection B'),'merge destination picker accepts keyboard selection');
    check(pickerTrigger?.getAttribute('aria-expanded')==='false','merge destination picker closes after selection');
    const mergeSection=[...document.querySelectorAll('.collection-manage-section')].find(section=>section.querySelector('.collection-picker'));
    mergeSection?.querySelector('.button-primary')?.click();
    check(document.querySelector('#workbench-collection-dialog')?.textContent.includes('Fixture Collection B'),'selected merge destination reaches the review step');
    check(document.querySelector('#workbench-collection-dialog').getAttribute('aria-labelledby')===document.querySelector('#workbench-collection-dialog [id$=title]')?.id,'merge review dialog exposes its title to assistive technology');
    document.querySelector('#workbench-collection-dialog button')?.click();
    applyLocale('en');document.documentElement.dataset.theme='dark';await wait(60);
    document.querySelector('.workbench-context-button')?.click();
    const darkPicker=document.querySelector('.collection-picker');
    const darkTrigger=darkPicker?.querySelector('[role=combobox]');
    check(darkPicker?.closest('dialog')?.open,'merge picker opens in the dark theme');
    check(darkPicker?.closest('dialog')?.textContent.includes('Merge into another Collection'),'merge labels update in English');
    darkTrigger?.click();darkPicker?.querySelector('[role=option]')?.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}));
    check(darkTrigger?.getAttribute('aria-expanded')==='false','Escape closes the picker in the dark theme');
    document.querySelector('#workbench-collection-dialog button')?.click();
    applyLocale('vi');document.documentElement.dataset.theme='light';await wait(40);
    window.__checkpoint='collection create';document.querySelector('#collection-create').click();
    let collectionForm=document.querySelector('#workbench-collection-dialog form');
    check(document.querySelector('#workbench-collection-dialog').getAttribute('aria-labelledby')===document.querySelector('#workbench-collection-dialog [id$=title]')?.id,'create dialog exposes its title to assistive technology');
    collectionForm.elements.namedItem('name').value='Fixture created Collection';
    collectionForm.requestSubmit();await wait(60);
    check(testRequests.some(request=>request.path==='/api/v1/collections'&&request.method==='POST'&&JSON.parse(request.body).name==='Fixture created Collection'),'Collection creation submits the entered name to its API');
    window.__checkpoint='collection edit open';document.querySelector('.workbench-context-button')?.click();await wait(20);
    document.querySelector('.collection-manage-actions button')?.click();await wait(20);
    collectionForm=document.querySelector('#workbench-collection-dialog form');
    check(document.querySelector('#workbench-collection-dialog').getAttribute('aria-labelledby')===document.querySelector('#workbench-collection-dialog [id$=title]')?.id,'edit dialog exposes its title to assistive technology');
    collectionForm.elements.namedItem('name').value='Fixture renamed Collection';
    collectionForm.requestSubmit();await wait(60);
    check(testRequests.some(request=>request.path==='/api/v1/collections/collection-a'&&request.method==='PATCH'&&JSON.parse(request.body).name==='Fixture renamed Collection'),'Collection editing saves the changed name');
    window.__checkpoint='collection delete open';document.querySelector('.workbench-context-button')?.click();await wait(20);
    document.querySelector('.collection-danger-button')?.click();await wait(20);
    check(document.querySelector('#workbench-collection-dialog').getAttribute('aria-labelledby')===document.querySelector('#workbench-collection-dialog [id$=title]')?.id,'delete confirmation exposes its title to assistive technology');
    document.querySelector('#workbench-collection-dialog .button-primary')?.click();await wait(60);
    check(testRequests.some(request=>request.path==='/api/v1/collections/collection-a'&&request.method==='DELETE'),'Collection deletion requires and submits a separate confirmation');
    window.__checkpoint='bulk assignment';const keywordCheck=document.querySelector('#workbench-rows tr input[type=checkbox]');
    if(keywordCheck){keywordCheck.click();document.querySelector('#workbench-action').value='ADD';document.querySelector('#workbench-action').dispatchEvent(new Event('change',{bubbles:true}));document.querySelector('#workbench-target').value='collection-b';document.querySelector('#workbench-bulk button:last-child').click();await wait(80);}
    check(testRequests.some(request=>request.path==='/api/v1/collections/bulk'&&request.method==='POST'&&JSON.parse(request.body).action==='ADD'&&JSON.parse(request.body).keyword_ids?.includes('c'.repeat(64))&&JSON.parse(request.body).collection_id==='collection-b'),'bulk assignment sends only the selected keyword to the chosen Collection');
    await go('runs');await wait(40);
    const historyButton=document.querySelector('#run-history button');
    check(Boolean(historyButton),'saved run history renders a details action');
    historyButton?.click();await wait(40);
    const historyDetail=document.querySelector('#run-history-detail');
    check(!historyDetail.hidden&&historyDetail.getAttribute('aria-labelledby')===historyDetail.querySelector('h2')?.id,'run detail is exposed as a named region');
    check(historyButton.getAttribute('aria-expanded')==='true'&&historyButton.getAttribute('aria-controls')==='run-history-detail','run detail action exposes expanded state and target');
    check(document.activeElement===historyDetail,'opening run details moves focus to the detail region');
    check(historyDetail.textContent.includes('Dữ liệu một phần')&&historyDetail.textContent.includes('Lượt xem trang Wikimedia'),'run detail localizes source and status values: '+historyDetail.textContent);
    check(historyDetail.textContent.includes('Dữ liệu mô phỏng')&&!historyDetail.textContent.includes('SYNTHETIC'),'run detail localizes the data mode');
    check(historyDetail.textContent.includes('Một hoặc nhiều nguồn chỉ trả về dữ liệu một phần')&&!historyDetail.textContent.includes('SOURCE_COVERAGE_PARTIAL')&&!historyDetail.textContent.includes('CENSORED_TOP_LIST')&&!historyDetail.textContent.includes('wikimedia_pageviews_top_per_country'),'run detail hides internal identifiers behind a user-facing explanation');
    applyLocale('en');
    check(historyDetail.textContent.includes('Partial data')&&historyDetail.textContent.includes('One or more sources returned partial data'),'open run detail relocalizes with current language');
    applyLocale('vi');
    await wait(40);
    const currentHistoryButton=document.querySelector('#run-history button[aria-controls=run-history-detail]');
    check(currentHistoryButton?.getAttribute('aria-expanded')==='true','run detail trigger state survives locale refresh');
    historyDetail.querySelector('button').click();
    check(historyDetail.hidden&&currentHistoryButton?.getAttribute('aria-expanded')==='false'&&document.activeElement===currentHistoryButton,'closing run detail restores focus to its current opener');
    history.replaceState(null,'',location.pathname+'?q=renewables#keywords');window.dispatchEvent(new Event('hashchange'));
    check(location.hash==='#collections','legacy keyword deep link redirects to Collections');
    check(new URLSearchParams(location.search).get('q')==='renewables','legacy keyword deep link preserves the search query');
    check(!document.querySelector('[data-view=collections]').hidden,'legacy keyword deep link opens the keyword workspace');
    check(document.querySelector('[data-view=overview]').hidden,'legacy keyword deep link does not expose a second explorer');
    check(location.search==='?q=renewables','legacy route preserves query string');
    history.replaceState(null,'',location.pathname+'#runs');window.dispatchEvent(new Event('hashchange'));
    document.querySelector('#run-id').value=RUN_A;document.querySelector('#load-run').click();await wait(180);
    check(document.querySelector('#runs-current').textContent===RUN_A,'run lookup');
    check(document.querySelector('#run-note').textContent.includes('Lần phân tích'),'Vietnamese dynamic run label');
    check(document.querySelector('#run-status').textContent.includes('một phần'),'partial run status');
    check(document.querySelector('#keyword-count').textContent==='1.234','Vietnamese number format');
    check(document.querySelector('#quality-coverage').textContent.includes('404'),'partial source coverage');
    check(document.querySelector('#quality-staleness').textContent.includes('3 ngày'),'stale observation');
    check(document.querySelector('#global-notice').textContent.includes('SYNTHETIC'),'synthetic mode warning');
    await go('quality');
    const qualityCopy=document.querySelector('[data-view=quality]').textContent;
    check(qualityCopy.includes('Số bản ghi hợp lệ / bị loại / trùng')&&qualityCopy.includes('Chưa có số liệu báo cáo'),'Vietnamese data-quality labels describe reported metrics without API jargon');
    check(qualityCopy.includes('Danh sách trang chỉ hiển thị một phần')&&qualityCopy.includes('không đồng nghĩa với 0 lượt xem'),'Vietnamese page-list limitation explains absence versus zero');
    applyLocale('en');
    const englishQualityCopy=document.querySelector('[data-view=quality]').textContent;
    check(englishQualityCopy.includes('Valid / rejected / duplicate record counts')&&englishQualityCopy.includes('No figures reported'),'English data-quality labels avoid implementation jargon');
    check(englishQualityCopy.includes('Partial page list')&&englishQualityCopy.includes('does not mean it had zero views'),'English page-list limitation explains absence versus zero');
    applyLocale('vi');

    await go('overview');await wait(20);
    check(document.querySelector('#rows').textContent.includes('<img'),'untrusted keyword rendered as text');
    check(!window.pwned&&!document.querySelector('#rows img'),'keyword XSS blocked');
    check(document.querySelector('#rows').textContent.includes('—'),'null score remains missing');
    const detailButton=document.querySelector('#rows button');
    detailButton.focus();detailButton.click();await wait(20);
    check(document.querySelector('#detail-dialog').open,'detail dialog opens');
    check(document.querySelector('#detail-content').textContent.includes('DAY_UNAVAILABLE'),'missing date evidence');
    check(!document.querySelector('#detail-content a'),'unsafe evidence link rejected');
    check(document.activeElement.closest('dialog')!==null,'dialog focus');
    check(document.querySelector('#detail-content').textContent.includes('Điểm / trạng thái'),'Vietnamese detail labels');
    applyLocale('en');
    check(document.querySelector('#detail-content').textContent.includes('Score / status'),'open detail relocalizes to English');
    applyLocale('vi');
    document.querySelector('#detail-dialog [data-close]').click();
    check(!document.querySelector('#detail-dialog').open,'dialog closes');
    check(document.activeElement===detailButton,'dialog restores focus');

    document.querySelector('#next').click();await wait(20);
    check(testRequests.some(request=>request.path.includes('offset=20')),'pagination request');
    const search=document.querySelector('#search');search.value='empty';
    search.dispatchEvent(new Event('input',{bubbles:true}));await wait(430);
    check(!document.querySelector('#empty').hidden,'empty search state');

    await go('research');
    check(Boolean(document.querySelector('#research-form')&&document.querySelector('#research-records')),'persistent research workflow form and saved-record list');
    check(document.querySelector('[data-view=research]').textContent.includes('nhu cầu thị trường'),'research page keeps commercial evidence boundaries visible');
    document.querySelector('#research-keyword').value='c'.repeat(64);
    document.querySelector('#research-subject').value='Fixture product idea';
    document.querySelector('#research-hypothesis').value='A testable hypothesis based on the saved trend signal.';
    document.querySelector('#research-category').value='Home organization';
    document.querySelector('#research-niche').value='Small apartments';
    document.querySelector('#research-competitors').value='Review products before drawing conclusions.';
    document.querySelector('#research-market-evidence').value='Evidence required before commercial validation.';
    document.querySelector('#research-ip').value='Check brand and IP evidence.';
    document.querySelector('#research-risks').value='Demand is not validated.';
    document.querySelector('#research-findings').value='Research is still in progress.';
    document.querySelector('#research-form').requestSubmit();await wait(80);
    check(testRequests.some(request=>request.path==='/api/v1/research'&&request.method==='POST'&&JSON.parse(request.body).subject==='Fixture product idea'&&JSON.parse(request.body).keyword_id==='c'.repeat(64)&&JSON.parse(request.body).market_evidence==='Evidence required before commercial validation.'),'Product Research links a saved keyword and records the entered hypothesis and market evidence');
    check(document.querySelector('#research-records .research-record h3')?.textContent==='Fixture product idea','saved hypothesis appears in the research list');
    const researchStatus=document.querySelector('#research-records select');researchStatus.value='RESEARCHING';researchStatus.dispatchEvent(new Event('change',{bubbles:true}));await wait(50);
    check(testRequests.some(request=>request.path==='/api/v1/research/research-fixture'&&request.method==='PATCH'&&JSON.parse(request.body).status==='RESEARCHING'),'Product Research status persists through its update action');
    check(!document.querySelector('[data-nav=refresh]'),'scheduler navigation is absent');
    check(!document.querySelector('[data-nav=usage]'),'unmeasured AI usage navigation is absent');
    await go('overview');
    const requestsBeforeTab=testRequests.length;
    document.querySelector('[data-dashboard-tab=analysis]').click();
    check(document.querySelector('[data-dashboard-tab=analysis]').getAttribute('aria-selected')==='true','Analysis semantic tab selected');
    check(new URLSearchParams(location.search).get('q')==='empty','tab route preserves the active query parameter');
    check(testRequests.length===requestsBeforeTab,'dashboard tab does not request data');
    document.querySelector('[data-dashboard-tab=overview]').focus();
    document.querySelector('[data-dashboard-tab=overview]').dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowRight',bubbles:true}));
    check(document.querySelector('[data-dashboard-tab=analysis]').getAttribute('aria-selected')==='true','ArrowRight changes tabs');
    await go('settings');
    const scheduleForm=document.querySelector('#settings-schedule-form');
    scheduleForm.elements.namedItem('name').value='Daily synthetic check';
    scheduleForm.elements.namedItem('operator-key').value='offline-scheduler-key-0123456789';
    scheduleForm.requestSubmit();await wait(60);
    check(testRequests.some(request=>request.path==='/api/v1/scheduler'&&request.method==='POST'&&JSON.parse(request.body).name==='Daily synthetic check'),'scheduler create submits through its protected API');
    check(Boolean(document.querySelector('#settings-schedules .schedule-edit')),'new disabled schedule appears in the saved list');
    document.querySelector('#settings-schedules .schedule-edit').click();
    check(scheduleForm.elements.namedItem('name').value==='Daily synthetic check'&&scheduleForm.elements.namedItem('time').value==='09:00','schedule edit loads saved values into the form');
    scheduleForm.elements.namedItem('name').value='Weekly synthetic review';
    scheduleForm.elements.namedItem('frequency').value='WEEKLY';
    scheduleForm.elements.namedItem('timezone').value='America/Los_Angeles';
    scheduleForm.elements.namedItem('time').value='10:30';
    scheduleForm.elements.namedItem('operator-key').value='offline-scheduler-key-0123456789';
    scheduleForm.requestSubmit();await wait(80);
    const schedulePatch=testRequests.find(request=>request.path==='/api/v1/scheduler/'+'e'.repeat(32)&&request.method==='PATCH');
    const scheduleBody=schedulePatch?JSON.parse(schedulePatch.body):{};
    check(scheduleBody.name==='Weekly synthetic review'&&scheduleBody.frequency==='WEEKLY'&&scheduleBody.timezone==='America/Los_Angeles'&&scheduleBody.hour===10&&scheduleBody.minute===30,'schedule edit persists frequency, timezone and run time');
    check(schedulePatch?.headers?.Authorization==='Bearer offline-scheduler-key-0123456789'&&scheduleForm.elements.namedItem('operator-key').value===''&&!Object.values(localStorage).some(value=>String(value).includes('offline-scheduler-key'))&&!Object.values(sessionStorage).some(value=>String(value).includes('offline-scheduler-key')),'schedule editing uses and clears a transient operator key');
    document.querySelector('[data-theme-toggle]').click();
    document.querySelector('[data-locale-toggle]').click();
    await wait(300);
    check(document.documentElement.dataset.theme==='dark','dark theme toggle');
    check(document.documentElement.lang==='en','English toggle');
    check(document.querySelector('#keyword-count').textContent==='1,234','English number format');
    check(document.querySelector('#run-note').textContent.includes('Run '),'English dynamic run label');
    check(document.querySelector('[data-nav=overview]').textContent.includes('Overview'),'English navigation translation');
    check(document.querySelector('#rows').closest('table').querySelector('th[data-i18n="table.topic"]').textContent.includes('TOPIC'),'English Discover table translation');

    document.querySelector('[data-locale-toggle]').click();
    check(document.documentElement.lang==='vi','switch back to Vietnamese');
    document.querySelector('[data-theme-toggle]').click();
    check(document.documentElement.dataset.theme==='light','switch back to light');
    document.querySelector('#run-id').value='d'.repeat(32);
    document.querySelector('#load-run').click();await wait(40);
    check(document.querySelector('#global-notice').textContent.includes('Không tìm thấy'),'read auth/not-found error visible');

    const apiUri=API_MODULE_URI;
    const {getJson}=await import(apiUri);
    try{await getJson('/api/auth-fail');check(false,'401 must fail closed');}
    catch(error){check(String(error).includes('không có quyền'),'localized 401');}
    try{await getJson('/api/status-403');check(false,'403 must fail closed');}
    catch(error){check(String(error).includes('bị từ chối'),'localized 403');}
    try{await getJson('/api/status-409');check(false,'409 must fail');}
    catch(error){check(String(error).includes('đang chạy'),'409 response');}
    try{await getJson('/api/status-429');check(false,'429 must fail');}
    catch(error){check(String(error).includes('giới hạn'),'429 response');}
    try{await getJson('/api/status-503');check(false,'5xx must fail');}
    catch(error){check(String(error).includes('Backend hoặc storage'),'localized 503 response');}
    try{await getJson('/api/status-500');check(false,'unmapped 5xx must fail');}
    catch(error){check(String(error).includes('HTTP 500'),'generic 5xx response');}
    try{await getJson('/api/network-fail');check(false,'network failure must fail');}
    catch(error){check(String(error).includes('Không kết nối'),'localized network failure');}
    try{await getJson('/api/invalid-json');check(false,'invalid JSON rejected');}
    catch(error){check(String(error).includes('không hợp lệ'),'invalid JSON message');}
    try{await getJson('/api/timeout-json',{timeoutMs:20});check(false,'request timeout');}
    catch(error){check(String(error).includes('quá thời gian'),'body timeout message');}
    const nativeTimeout=window.setTimeout;
    window.setTimeout=(callback,delay,...args)=>nativeTimeout(callback,delay===2000?0:delay,...args);
    document.querySelector('#run-id').value='b'.repeat(32);document.querySelector('#load-run').click();await wait(120);
    check(testRequests.filter(request=>request.path==='/api/runs/'+'b'.repeat(32)).length===31,'bounded run polling');
    check(document.querySelector('#global-notice').textContent.includes('30 lần kiểm tra'),'polling stop status');
    window.setTimeout=nativeTimeout;
    const writes=testRequests.filter(request=>request.method!=='GET');
    check(writes.length===8&&writes.every(request=>[
      'POST /api/v1/collections','PATCH /api/v1/collections/collection-a',
      'DELETE /api/v1/collections/collection-a','POST /api/v1/collections/bulk',
      'POST /api/v1/research','PATCH /api/v1/research/research-fixture',
      'POST /api/v1/scheduler','PATCH /api/v1/scheduler/'+'e'.repeat(32)
    ].includes(request.method+' '+request.path)),'offline browser writes are limited to explicitly exercised Collection, Research, and Scheduler fixture actions');
    check(!testRequests.some(request=>request.path.startsWith('/api/v1/fetches')),'offline browser workflow did not submit a provider Fetch');
    check(!testRequests.some(request=>/token|secret|credential/i.test(JSON.stringify(request.headers))),'no credential header');
    check(!testRequests.some(request=>request.path==='/api/config'),'no auth-capability inference from config');
    check(localStorage.getItem('trendscout.theme')==='light','light preference persisted');
    check(localStorage.getItem('trendscout.locale')==='vi','Vietnamese preference persisted');
    document.querySelector('[data-theme-toggle]').click();
    document.querySelector('[data-locale-toggle]').click();
    check(localStorage.getItem('trendscout.theme')==='dark','theme saved');
    check(localStorage.getItem('trendscout.locale')==='en','locale saved');
    check(!Object.keys(localStorage).some(key=>/token|secret|credential/i.test(key)),'no credential storage');
    check(document.documentElement.scrollWidth<=innerWidth+1,'document horizontal overflow');
  }catch(error){errors.push('exception at '+(window.__checkpoint||'initial workflow')+': '+String(error));}
  document.body.setAttribute('data-browser-result',errors.length?'FAIL: '+errors.join('; '):'PASS');
})();
</script>"""


def main():
    require_linux_network_isolation()
    chrome = find_chrome()
    if not chrome:
        raise SystemExit("Chrome unavailable")
    html = (PUBLIC / "index.html").read_text(encoding="utf-8")
    css = (PUBLIC / "styles.css").read_text(encoding="utf-8")
    invalid_preferences = """<script>try{localStorage.setItem('trendscout.theme','sepia');localStorage.setItem('trendscout.locale','fr')}catch{}</script>"""
    html = html.replace('href="/dashboard/styles.css"', "<style>" + css + "</style>")
    html = html.replace('src="/dashboard/app.js"', 'src="' + (PUBLIC / "app.js").as_uri() + '"')
    check = CHECK.replace("RUN_A", json.dumps("a" * 32))
    check = check.replace("API_MODULE_URI", json.dumps((PUBLIC / "modules" / "api.js").as_uri()))
    check = check.replace("I18N_MODULE_URI", json.dumps((PUBLIC / "modules" / "i18n.js").as_uri()))
    with tempfile.TemporaryDirectory(prefix="trendscout-browser-") as directory:
        page = Path(directory) / "index.html"
        for width in (320, 390, 768, 1024, 1440, 1920):
            page_html = html
            if width == 768:
                page_html = page_html.replace(
                    "<script>\n    (() => {", invalid_preferences + "<script>\n    (() => {", 1
                )
            width_check = check.replace(
                "INVALID_PREFERENCE_TEST", "true" if width == 768 else "false"
            ).replace("RELOAD_TEST", "true" if width == 320 else "false")
            page.write_text(page_html.replace("</body>", FAKE + width_check + "</body>"),
                            encoding="utf-8")
            command = [
                chrome,
                "--headless",
                "--no-sandbox",
                "--disable-gpu",
                "--disable-background-networking",
                "--disable-extensions",
                "--no-proxy-server",
                "--disable-sync",
                "--disable-component-update",
                "--allow-file-access-from-files",
                "--no-first-run",
                "--user-data-dir=" + directory + "/profile-" + str(width),
                "--window-size=" + str(width) + ",1000",
                "--virtual-time-budget=24000",
                "--dump-dom",
                page.as_uri(),
            ]
            env = {
                key: value
                for key, value in os.environ.items()
                if key.lower() not in ("http_proxy", "https_proxy", "all_proxy", "no_proxy")
            }
            result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                                    errors="replace", timeout=40, env=env)
            if result.returncode or 'data-browser-result="PASS"' not in result.stdout:
                marker = (
                    result.stdout.split("data-browser-result=", 1)[-1][:800]
                    if "data-browser-result=" in result.stdout
                    else "missing browser result: " + result.stdout[-1200:]
                )
                raise SystemExit(
                    "Chrome "
                    + str(width)
                    + " failed: "
                    + marker
                    + " (exit "
                    + str(result.returncode)
                    + ")"
                )
            print(
                "Chrome "
                + str(width)
                + "px, locale/theme, all views, Source Center search, Collection CRUD/bulk and Research fixtures, no Fetch, XSS: PASS"
            )


if __name__ == "__main__":
    main()
