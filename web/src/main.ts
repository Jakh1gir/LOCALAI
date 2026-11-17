import { mount } from 'svelte';
import App from './App.svelte';
import './shell.css';

mount(App, { target: document.getElementById('app')! });
