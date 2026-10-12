import component from './Settings.svelte';
import type {SettingsContribution} from '../../lib/settingsTypes';
const contribution:SettingsContribution={
  group:{id:'twitter',label:'Twitter',module:'twitter'},order:5,component,
  sections:[{id:'twitter-capture',group:'twitter',label:'Capture',icon:'folder'}],
  searchItems:[{id:'twitter-destination',section:'twitter-capture',label:'Save to',description:'',keywords:['twitter','download','capture','folder','destination','save']}],
};
export default contribution;
