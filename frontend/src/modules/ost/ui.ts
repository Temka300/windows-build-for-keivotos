import type {ModuleUiDescriptor} from '../registry';
import {observeMusicMetadata} from './metadataNotifications';
// Contribution discovery remains inert until the enabled-owner list is loaded.
if(typeof window!=='undefined')observeMusicMetadata();
const descriptor:ModuleUiDescriptor={slug:'ost',iconSrc:null,drawerActions:[]};
export default descriptor;
