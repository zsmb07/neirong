// Run with: node --test tests/test_military_dock.cjs
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const html=fs.readFileSync(path.join(__dirname,'..','dashboard.html'),'utf8');
const quoteSource=html.slice(html.indexOf('function militaryRecruitQuote('),html.indexOf('function renderMilitaryCommand('));
function setup(){
  const c={treasury:40,food:100};
  const option={unlocked:true,room:12,cost_treasury:12.5,cost_food:3,reason:'财政不足'};
  const context={STATE:{military_specs:{recruit_batch_max:4,barracks:{garrison_capacity:4}}}};
  vm.createContext(context);vm.runInContext(quoteSource,context);
  return {c,option,quote:context.militaryRecruitQuote};
}
test('a player who cannot afford four units can still recruit one',()=>{
  const {c,option,quote}=setup();
  assert.equal(quote(c,null,option,1).enabled,true);
  assert.equal(quote(c,null,option,4).enabled,false);
  assert.equal(quote(c,null,option,4).cost,50);
  assert.equal(quote(c,null,option,4).food,12);
});
test('technology, national roster, camp capacity and food gates remain visible',()=>{
  const {c,option,quote}=setup();
  assert.equal(quote(c,null,{...option,unlocked:false,reason:'需要科技'},1).reason,'需要科技');
  assert.equal(quote(c,null,{...option,room:0},1).enabled,false);
  assert.equal(quote(c,{tile_room:0},option,1).reason,'驻格容量不足');
  assert.equal(quote({...c,food:2},null,option,1).reason,'粮食不足');
  assert.equal(quote(c,{barracks:false,strength:4},option,1).enabled,true);
});
function dock(paused){
  function parent(){return {appendChild(node){node.parentNode=this;}};}
  const home=parent(),host=parent();
  const nodes={'panel-tile':{parentNode:home},militaryTileHost:host,militaryModal:{hidden:true}};
  const classes=new Set(),calls=[];
  const context={STATE:{paused},selectedTile:null,selectedCountry:'A',militaryResume:false,militaryTileHome:null,
    $:id=>nodes[id],countryMeta:()=>({capital:{col:2,row:3}}),
    document:{body:{classList:{add:x=>classes.add(x),remove:x=>classes.delete(x)}}},
    setRail:()=>{},showToast:()=>{},renderRight:()=>{},renderMilitary:()=>{},resize:()=>{},
    control:async action=>{calls.push(action);}};
  vm.createContext(context);
  vm.runInContext(html.slice(html.indexOf('async function openMilitary(){'),html.indexOf('function renderMilitary(){')),context);
  return {context,nodes,home,host,classes,calls};
}
test('opening and closing reuses the same tile card and restores its original parent',async()=>{
  const {context,nodes,home,host,classes,calls}=dock(true);
  const tile=nodes['panel-tile'];
  await context.openMilitary();
  assert.equal(tile.parentNode,host);assert.equal(nodes.militaryModal.hidden,false);
  assert.equal(classes.has('military-open'),true);
  assert.equal(context.selectedTile.join(','),'2,3');
  await context.closeMilitary();
  assert.equal(tile.parentNode,home);assert.equal(nodes.militaryModal.hidden,true);
  assert.equal(classes.size,0);assert.deepEqual(calls,[]);
});
test('a running game pauses for orders and resumes on leaving',async()=>{
  const {context,calls}=dock(false);
  context.selectedTile=[7,8];
  await context.openMilitary();await context.closeMilitary();
  assert.deepEqual(calls,['pause','resume']);
  assert.equal(context.selectedTile.join(','),'7,8');
});
test('military dock retains all existing advanced commands',()=>{
  for(const command of ['data-camp-toggle','data-camp-target','data-siege-city','data-siege-march',
    'data-mass','data-field-strike','data-mil-peace','data-build','data-march-army'])assert.ok(html.includes(command),command);
  assert.ok(html.includes("!spec.on_city_center||tileIsCenter(tile)"));
  assert.ok(html.includes('data-building="BARRACKS"'));
  assert.ok(html.includes('data-building="WALL"'));
});


test('rendezvous creates one slider per unit, revalidates on input and only confirms one order',async()=>{
  const nodes=new Map();
  const element=()=>({children:[],open:false,disabled:true,append(...children){this.children.push(...children);},
    replaceChildren(){this.children=[];},setAttribute(){},showModal(){this.open=true;},close(){this.open=false;}});
  const get=id=>{if(!nodes.has(id))nodes.set(id,element());return nodes.get(id);};
  const previews=[],orders=[],pending=[];
  const quote={room:3,source_strength:7,maximum:3,source_units:{MILITIA:5,ARCHER:2},
    source_labels:{MILITIA:'民兵',ARCHER:'弓兵'}};
  const context={$:get,document:{createElement:element},
    fetch:async(url,options)=>{assert.equal(url,'/api/rendezvous_preview');previews.push(JSON.parse(options.body));
      if(previews.length>1)return new Promise(resolve=>pending.push(resolve));
      return {json:async()=>({ok:true,quote:{...quote,errors:[{text:'至少选择 1 支兵'}]}})};
    },
    submitPlayerAction:async action=>{orders.push(JSON.parse(JSON.stringify(action)));return true;}};
  vm.createContext(context);
  vm.runInContext(html.slice(html.indexOf('async function openRendezvous('),html.indexOf('let militaryTileHome=')),context);
  await context.openRendezvous('A1','A2');
  const slider=get('rendezvousSliders').children.map(label=>label.children[1]);
  assert.deepEqual(slider.map(input=>input.max),['5','2']);
  assert.match(get('rendezvousTotals').textContent,/还能装 3.*共 7.*最多可派 3/);
  slider[0].value='4';slider[0].oninput();
  slider[1].value='1';slider[1].oninput();
  assert.deepEqual(previews.map(p=>p.units),[{},{MILITIA:4},{MILITIA:4,ARCHER:1}]);
  const errors=[{text:'源军兵种名额到底'},{text:'装不下，还能装 3 支'},{text:'宿主兵营同兵种名额已满'}];
  pending[1]({json:async()=>({ok:true,quote:{...quote,errors}})});
  await new Promise(setImmediate);
  assert.match(get('rendezvousErrors').textContent,/源军兵种名额到底.*还能装 3.*宿主兵营同兵种名额/);
  pending[0]({json:async()=>({ok:true,quote:{...quote,errors:[]}})});
  await new Promise(setImmediate);
  assert.equal(get('rendezvousConfirm').disabled,true);
  assert.equal(orders.length,0);
  slider[0].value='0';slider[0].oninput();
  pending[2]({json:async()=>({ok:true,quote:{...quote,errors:[]}})});
  await new Promise(setImmediate);
  await get('rendezvousConfirm').onclick();
  assert.equal(orders.length,1);
  assert.deepEqual(orders[0].units,{MILITIA:0,ARCHER:1});
  assert.equal(get('rendezvousDialog').open,false);
});


test('tile shows the intent before exposing individual army rows',()=>{
  const context={STATE:{player_country:'A'},tileGarrisonAt:()=>[],countryMeta:id=>({name:id}),esc:x=>x};
  vm.createContext(context);
  const march=html.slice(html.indexOf('function tileMarchCard('),html.indexOf('function tileFieldCard('));
  const attack=html.slice(html.indexOf('function tileStrikeCard('),html.indexOf('function hostilePaceText('));
  vm.runInContext(march+attack,context);
  const tile={col:1,row:2,land:true,owner:null};
  const pricing={march:{rows:[{army_id:'A1',days:6}]},field:[],garrison:[]};
  assert.match(context.tileMarchCard(tile,pricing),/驻扎/);
  assert.doesNotMatch(context.tileMarchCard(tile,pricing),/A1|6 日/);
  pricing.garrison.push({country_id:'A',strength:3});
  assert.match(context.tileMarchCard(tile,pricing),/会师/);
  assert.match(context.tileStrikeCard({...tile,owner:'B'},{at_war:false}),/宣战/);
  assert.match(context.tileStrikeCard({...tile,owner:'B'},{at_war:true,strikes:[{army_id:'A1'}]}),/攻打/);
});
