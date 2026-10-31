import type { ModuleUiDescriptor } from '../registry';
import {mangaApi} from './api';
const descriptor: ModuleUiDescriptor = {slug:'manga', iconSrc:null, drawerActions:[], categories:mangaApi.categories};
export default descriptor;
