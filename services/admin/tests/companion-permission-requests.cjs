// Execute each shipped view with a small Obsidian host and emit its startup requests.
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const {webcrypto}=require('node:crypto');
class El{constructor(){this.children=[];this.style={};}empty(){this.children=[];}createEl(){const e=new El();this.children.push(e);return e;}createDiv(){return this.createEl();}setText(){}addClass(){}}
let descriptions=[];
class Setting{setName(){return this;}setDesc(text){descriptions.push(text);return this;}addToggle(f){const c={setValue(){return c;},onChange(){return c;}};f(c);return this;}addText(f){const c={setValue(){return c;},onChange(){return c;}};f(c);return this;}addComponent(){return this;}addButton(f){const c={setButtonText(){return c;},setCta(){return c;},onClick(){return c;}};f(c);return this;}}
const calls=[],views={};
class Plugin{registerView(t,f){views[t]=f;}addSettingTab(tab){this.tab=tab;}addCommand(){}addRibbonIcon(){}registerEvent(){}async loadData(){return {url:'http://api',secret:'test'};}}
class ItemView{constructor(leaf){this.app=leaf.app;this.contentEl=new El();}}
const file={basename:'Meeting',path:'Review/Meeting.md'};
const app={workspace:{getActiveFile:()=>file,on:()=>null},vault:{read:async()=>''},metadataCache:{getFileCache:()=>({frontmatter:{id:'synthetic-note'}})},secretStorage:{getSecret:()=> 'synthetic'}};
const requestUrl=async req=>{calls.push({method:req.method,path:new URL(req.url).pathname+new URL(req.url).search});
  if(req.url.endsWith('/v1/schema'))return {status:200,json:{roles:{id_key:'id'}}};
  if(req.url.endsWith('/review'))return {status:200,json:{note:{content_hash:'',frontmatter:{reviewed:true}},roles:{reviewed_key:'reviewed',type_key:'type'}}};
  if(req.url.endsWith('/waiting'))return {status:200,json:{meetings:[],failed:[]}};
  return {status:200,json:{enabled:false,items:[],unknowns:[],people:[]}};};
const context={module:{exports:{}},require:()=>({Plugin,ItemView,Setting,PluginSettingTab:class{constructor(){this.containerEl=new El();}},SecretComponent:class{},Notice:class{},requestUrl,MarkdownView:class{},Modal:class{}}),crypto:webcrypto,TextEncoder,URL,setInterval:()=>0,clearInterval:()=>{}};
vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../coppermind_admin/obsidian_plugin/main.js'),'utf8'),context);
(async()=>{const plugin=new context.module.exports();plugin.app=app;await plugin.onload();plugin.tab.display();for(const type of Object.keys(views)){const before=calls.length;await views[type]({app}).onOpen();if(calls.length===before)throw Error(type+' made no request');}process.stdout.write(JSON.stringify({calls,descriptions}));})().catch(e=>{process.stderr.write(String(e));process.exitCode=1;});
