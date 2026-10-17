/* Run with Node.js. No browser or employer access is used. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {randomUUID} = require('node:crypto');

const html = fs.readFileSync(path.join(__dirname, 'index.html'), 'utf8');
const source = html.split('<script>')[1].split('</script>')[0]
  .replace('renderNav();refresh({initial:true});', '');
const fixture = () => {
  const companies = ['a', 'b'].map(id => ({id, name:id.toUpperCase(), revision:1, excluded:false, data:{}}));
  const jobs = ['1','2','3','4'].map((id,i)=>({key:`${i<3?'a':'b'}:${id}`, company_id:i<3?'a':'b', job_id:id, revision:1, decision:'pending', data:{title:`Job ${id}`, url:`https://example.com/${id}`}}));
  return {token:'test', companies, jobs, queue:[], imports:[], card_reviews:[], flashcards:[{company_id:'a',revision:1,jobs:jobs.slice(0,3),remaining_jobs:3},{company_id:'b',revision:1,jobs:jobs.slice(3),remaining_jobs:1}]};
};
function harness(fetch) {
  const nodes = new Map();
  const node = id => {
    if (!nodes.has(id)) nodes.set(id,{id, value:'', innerHTML:'', disabled:false, dataset:{}, style:{setProperty(){},removeProperty(){}}, classList:{toggle(){},add(){},remove(){}}, setAttribute(){}, addEventListener(){}, contains(){return false;}, focus(){}, scrollIntoView(){}});
    return nodes.get(id);
  };
  const listeners = new Map();
  const storage = new Map();
  const localStorage = {getItem:key=>storage.get(key)||null,setItem:(key,value)=>storage.set(key,value),removeItem:key=>storage.delete(key)};
  const context = vm.createContext({console, URL, crypto:{randomUUID}, localStorage, sessionStorage:localStorage, fetch,
    document:{getElementById:node,addEventListener(type,listener){if(!listeners.has(type))listeners.set(type,[]);listeners.get(type).push(listener);},querySelectorAll(){return [];},activeElement:null},
    window:{scrollY:0,addEventListener(){},scrollTo(){}}, setTimeout, clearTimeout, requestAnimationFrame:fn=>fn()});
  vm.runInContext(source,context);
  context.fixture = fixture();
  vm.runInContext("state=fixture;online=true;currentCard={...state.flashcards[0],company:{...state.companies[0]},jobs:state.flashcards[0].jobs.map(j=>({...j,data:{...j.data}}))};cardSelected.add('a:1');render=()=>{};",context);
  return {context,storage,node,listeners,run:code=>vm.runInContext(code,context)};
}
async function settled(h) {await h.run('chain');await Promise.resolve();}
function successfulHarness(){
  const calls=[];
  const h=harness(async(url,init)=>{
    const payload=JSON.parse(init.body);calls.push({url,payload});
    const result=fixture();
    for(const key of payload.selected||[]){const j=result.jobs.find(item=>item.key===key);j.decision='approve';j.revision++;}
    if(payload.action==='exclude')result.companies[0].excluded=true;
    result.flashcards=result.flashcards.slice(1);
    result.card_reviews=[{id:payload.request_id,company_id:payload.company_id,action:payload.action,selected:payload.selected}];
    return {ok:true,json:async()=>result};
  });
  return {...h,calls};
}
function pointerSurface(h){
  const classes=new Set(),feedback={textContent:''};
  let captured=null;
  const card={
    dataset:{},style:{transform:''},
    classList:{add:name=>classes.add(name),remove:name=>classes.delete(name)},
    getBoundingClientRect:()=>({width:400}),
    querySelector:selector=>selector==='.swipe-feedback'?feedback:null,
    setPointerCapture:id=>{captured=id;},
    hasPointerCapture:id=>captured===id,
    releasePointerCapture:()=>{captured=null;},
  };
  // Model closest() for a plain card child or a child of an interactive element.
  const target=(ancestor='div')=>({closest(selector){
    if(selector==='.company-card')return card;
    const options=selector.split(',').map(s=>s.trim());
    return options.includes(ancestor)?this:null;
  }});
  function emit(type,properties={}){
    const event={pointerId:7,isPrimary:true,button:0,clientX:0,clientY:0,target:target(),cancelable:true,defaultPrevented:false,
      preventDefault(){this.defaultPrevented=true;},...properties};
    const handlers=h.listeners.get(type)||[];
    assert.ok(handlers.length,`${type} is connected to the gesture controller`);
    for(const handler of handlers)handler(event);
    return event;
  }
  return {card,classes,feedback,target,emit,captured:()=>captured};
}
async function gestureChecks(){
  const pure=successfulHarness();
  for(const [dx,dy,width,expected] of [
    [0,0,400,null],[20,2,400,null],[109,0,400,null],
    [111,0,400,'approve'],[-111,0,400,'exclude'],
    [69,0,200,null],[71,0,200,'approve'],
    [109,0,1000,null],[111,0,1000,'approve'],
    [200,300,400,null],[-200,300,400,null],
    [130,100,400,null],[150,100,400,'approve'],[-150,100,400,'exclude'],
  ])assert.equal(pure.run(`swipeIntent(${dx},${dy},${width})`),expected,`intent for ${dx},${dy} on width ${width}`);

  const empty=successfulHarness();
  empty.run("cardSelected.clear();commitSwipe('approve')");await settled(empty);
  assert.equal(empty.calls.length,0,'right swipe without a checked job sends nothing');
  assert.equal(empty.run('pendingCard'),null);
  assert.equal(empty.run('currentCard.company_id'),'a');
  assert.match(empty.run('notice?.message||\'\''),/pick|select|check/i,'empty selection gives a useful instruction');

  for(const [reason,setup] of [
    ['saving','busy=true'],['refreshing','loading=true'],['offline','online=false'],
    ['unconfirmed request',"pendingCard={request_id:'prior-request'}"],
    ['unsaved note',"noteDrafts.set('job:a:1','Keep this note')"],
    ['stale job','state.flashcards[0].jobs[0].revision=2'],
    ['stale company','state.flashcards[0].revision=2'],
    ['removed cohort','state.flashcards=[]'],['no current card','currentCard=null'],
    ['another view',"view='all'"],
  ]){
    const locked=successfulHarness();locked.run(setup);
    locked.run("commitSwipe('approve');commitSwipe('exclude')");await settled(locked);
    assert.equal(locked.calls.length,0,`${reason} blocks both swipe decisions`);
    assert.equal(locked.run('state.jobs[0].decision'),'pending',`${reason} cannot change a local decision`);
  }

  const approve=successfulHarness();
  approve.run("cardSelected.clear();cardSelected.add('a:1');cardSelected.add('a:3');commitSwipe('approve')");await settled(approve);
  assert.equal(approve.calls.length,1);
  assert.equal(approve.calls[0].url,'/api/card','gesture uses the existing durable card endpoint');
  assert.equal(approve.calls[0].payload.action,'approve');
  assert.deepEqual(approve.calls[0].payload.selected,['a:1','a:3'],'gesture approves only checked jobs');
  assert.deepEqual(approve.calls[0].payload.jobs,[{key:'a:1',revision:1},{key:'a:2',revision:1},{key:'a:3',revision:1}],'request retains the exact displayed cohort revisions');
  assert.equal(approve.run('state.jobs[1].decision'),'pending','unchecked job remains pending');
  assert.equal(approve.run('state.jobs[3].decision'),'pending','another company is unaffected');
  assert.equal(approve.run('currentCard'),null,'confirmed gesture save permits the next card');

  const exclude=successfulHarness();
  exclude.run("commitSwipe('exclude')");await settled(exclude);
  assert.equal(exclude.calls.length,1);
  assert.equal(exclude.calls[0].payload.company_id,'a');
  assert.equal(exclude.calls[0].payload.action,'exclude');
  assert.deepEqual(exclude.calls[0].payload.selected,[],'left swipe cannot approve checked jobs');

  const interactive=['a','button','input','label','textarea','select','summary','details','[contenteditable="true"]','[role="button"]'];
  for(const ancestor of interactive){
    const h=successfulHarness(),p=pointerSurface(h);h.context.origin=p.target(ancestor);
    assert.equal(h.run('swipeOriginAllowed(origin)'),false,`${ancestor} cannot start a swipe`);
    p.emit('pointerdown',{target:p.target(ancestor)});
    p.emit('pointermove',{clientX:180});p.emit('pointerup',{clientX:180});await settled(h);
    assert.equal(h.calls.length,0,`${ancestor} interaction cannot save a decision`);
  }

  const noSaveCases=[
    ['short drag',p=>{p.emit('pointerdown');p.emit('pointermove',{clientX:45,clientY:2});p.emit('pointerup',{clientX:45,clientY:2});}],
    ['vertical scroll',p=>{p.emit('pointerdown');const move=p.emit('pointermove',{clientX:3,clientY:30});assert.equal(move.defaultPrevented,false,'vertical scroll remains native');p.emit('pointermove',{clientX:200,clientY:40});p.emit('pointerup',{clientX:200,clientY:40});}],
    ['diagonal drag',p=>{p.emit('pointerdown');p.emit('pointermove',{clientX:130,clientY:100});p.emit('pointerup',{clientX:130,clientY:100});}],
    ['pointer cancellation',p=>{p.emit('pointerdown');p.emit('pointermove',{clientX:180});p.emit('pointercancel');p.emit('pointerup',{clientX:180});}],
    ['lost capture',p=>{p.emit('pointerdown');p.emit('pointermove',{clientX:180});p.emit('lostpointercapture');p.emit('pointerup',{clientX:180});}],
    ['secondary mouse button',p=>{p.emit('pointerdown',{button:2});p.emit('pointermove',{clientX:180});p.emit('pointerup',{clientX:180});}],
    ['non-primary pointer',p=>{p.emit('pointerdown',{isPrimary:false});p.emit('pointermove',{clientX:180});p.emit('pointerup',{clientX:180});}],
    ['second touch',p=>{p.emit('pointerdown');p.emit('pointermove',{clientX:180});p.emit('pointerdown',{pointerId:8,isPrimary:false});p.emit('pointerup',{clientX:180});}],
  ];
  for(const [reason,perform] of noSaveCases){
    const h=successfulHarness(),p=pointerSurface(h);perform(p);await settled(h);
    assert.equal(h.calls.length,0,`${reason} does not save`);
    assert.equal(h.run('swipe'),null,`${reason} clears gesture state`);
    assert.equal(p.card.style.transform,'',`${reason} clears card movement`);
    assert.equal(p.classes.has('is-dragging'),false,`${reason} clears drag styling`);
    assert.equal(p.captured(),null,`${reason} releases pointer capture`);
  }

  for(const [reason,change] of [
    ['save starts','busy=true'],['refresh starts','loading=true'],['goes offline','online=false'],
    ['request becomes pending',"pendingCard={request_id:'other-request'}"],
    ['posting becomes stale','state.flashcards[0].jobs[0].revision=2'],
    ['displayed card changes',"currentCard={...state.flashcards[1],company:state.companies[1]}"],
  ]){
    const h=successfulHarness(),p=pointerSurface(h);
    p.emit('pointerdown');p.emit('pointermove',{clientX:160});h.run(change);p.emit('pointerup',{clientX:160});await settled(h);
    assert.equal(h.calls.length,0,`${reason} during drag prevents save`);
    assert.equal(h.run('swipe'),null,`${reason} ends the drag safely`);
  }

  const dragged=successfulHarness(),surface=pointerSurface(dragged);
  dragged.run("cardSelected.add('a:3')");surface.emit('pointerdown');
  const movement=surface.emit('pointermove',{clientX:160,clientY:12});
  assert.equal(movement.defaultPrevented,true,'a deliberate horizontal drag takes control');
  assert.equal(dragged.calls.length,0,'movement alone never saves');
  surface.emit('pointerup',{clientX:160,clientY:12});await settled(dragged);
  assert.deepEqual(dragged.calls[0].payload.selected,['a:1','a:3'],'complete pointer gesture submits the exact checked jobs');
  assert.equal(dragged.calls.length,1);
  assert.equal(surface.card.style.transform,'','completed drag clears visual state');
  assert.equal(surface.feedback.textContent,'','completed drag clears its preview label');
  assert.equal(surface.captured(),null,'completed drag releases pointer capture');
}
async function main(){
  let calls=[];
  const failed=harness(async(url,init)=>{calls.push(JSON.parse(init.body));return {ok:false,status:400,json:async()=>({error:'Cannot save this choice.'})};});
  failed.run("submitCard('approve');submitCard('exclude');");await settled(failed);
  assert.equal(calls.length,1,'a second click cannot send another decision');
  assert.equal(failed.run('currentCard.company_id'),'a','failed save must keep the same card');
  assert.equal(failed.run('state.jobs[0].decision'),'pending','a failed save cannot approve a job locally');
  assert.equal(failed.run('pendingCard'),null,'a confirmed rejection clears retry state');

  calls=[];
  let loseResponse=true;
  const lost=harness(async(url,init)=>{
    const payload=JSON.parse(init.body);calls.push(payload);
    if(loseResponse)throw new Error('Connection interrupted after write');
    const result=fixture();result.jobs[0].decision='approve';result.jobs[0].revision=2;
    result.flashcards=result.flashcards.slice(1);result.card_reviews=[{id:payload.request_id,company_id:'a',action:'approve',selected:['a:1']}];
    return {ok:true,json:async()=>result};
  });
  lost.run("submitCard('approve')");await settled(lost);
  assert.equal(lost.run('currentCard.company_id'),'a','lost response must not advance');
  assert.equal(lost.run('online'),false,'unconfirmed save disables further edits');
  const request=JSON.parse(lost.storage.get('job-inbox-pending-card-v1'));
  assert.equal(request.selected.join(','),'a:1','only the selected exact job is sent');
  loseResponse=false;lost.run('online=true;submitCard(null,true)');await settled(lost);
  assert.deepEqual(calls[1],calls[0],'recovery repeats the same idempotent request');
  assert.equal(lost.run('currentCard'),null,'confirmed save releases the card for next render');
  assert.equal(lost.run('state.flashcards[0].company_id'),'b');
  assert.equal(lost.run('state.jobs[1].decision'),'pending','unselected jobs stay pending');
  assert.equal(lost.storage.has('job-inbox-pending-card-v1'),false);

  const stale=harness(async(url,init)=>{
    if(init?.method==='POST')return {ok:false,status:409,json:async()=>({error:'Posting changed.'})};
    const result=fixture();result.jobs[0].revision=2;return {ok:true,json:async()=>result};
  });
  stale.run("submitCard('approve')");await settled(stale);
  assert.equal(stale.run('currentCard.jobs[0].revision'),1,'stale view keeps the shown cohort');
  assert.equal(stale.run('freshCard()'),false,'stale cohort must require an explicit reload');
  assert.equal(stale.run('cardActionDisabled()'),true);

  const empty=harness(async()=>{throw new Error('Should not send');});
  empty.run("cardSelected.clear();submitCard('approve')");
  assert.equal(empty.run('pendingCard'),null,'empty selection cannot approve jobs');
  assert.equal(empty.run('view'),'cards','first upgrade opens the new review view');
  await gestureChecks();
  console.log('UI checks passed: failed/lost response recovery, exact selections, stale/locked cards, swipe thresholds and direction, scroll/cancel/interactive-origin safety, pointer completion, default view.');
}
main().catch(error=>{console.error(error);process.exitCode=1;});
