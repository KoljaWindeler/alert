import time,json,os,base64,hashlib,string,random, subprocess, traceback
from clients import alert_event,webcam_viewer,det_state
import server_m2m
import server_ws2 as server_ws
import send_mail
import p
from clients import m2m_clients
from rule_manager import *
from sql import *
from debug import *

#***************************************************************************************#
#***************************************** m2m *****************************************#
#***************************************************************************************#
# M2M functions are used to handle all messages between the server machine and the
# camera machine, M<->M. There should be no user interaction involved, as every message
# shall go over this server.
# There is a thread called server_m2m running in the background and waiting for incoming
# packets (low level). Whenever new camera is coming online the function recv_m2m_con_q_handle
# shall be triggered. This function will queue the data to recv_m2m_con_q and finish.
#
# The main loop shall run the function recv_m2m_con_dq_handle and check if our queue has
# elements to be processed. if this is the case the function will call recv_m2m_con_handle
# to process the connection change.
#
# The same principal is used to receive messages from the clients
#
# If recv_m2m_con_handle or recv_m2m_msg_handle or anyone else has to send something back
# to the cam, then they should append a message to the msq_q_m2m structure
# This structure will be checked by the main loop as well and if it contains some they
# will be send to the correct m2m device mentioned in the CLI argument. This way all messages
# and state changes are aligned in FIFO structures
#******************************************************#

################## M2M CONNECTION #########################
#******************************************************#
# recv_m2m_con_q_handle will be called by the server structure and shall just append
# the message to the queue
def recv_m2m_con_q_handle(data,m2m):
	recv_m2m_con_q.append((data,m2m))
#******************************************************#

#******************************************************#
# recv_m2m_con_dq_handle will be called in the main loop and will forward a message
# to the recv_m2m_con_handle if there is one in the queue
def recv_m2m_con_dq_handle():
	ret=0
	if(len(recv_m2m_con_q)>0):
		ret=1
		recv_con=recv_m2m_con_q[0]
		recv_m2m_con_q.remove(recv_con)
		try:
			recv_m2m_con_handle(recv_con[0],recv_con[1])
		except:
			p.err("sys:")
			p.err(str(sys.exc_info()[0]))
			p.err(str(sys.exc_info()[1]))
			p.err(str(repr(traceback.format_tb(sys.exc_info()[2]))))
	return ret
#******************************************************#

#******************************************************#
# recv_m2m_con_handle will be called by the dequeue handle above.
# it shall handle disconnect situations to avoid that we are talking to dead sockets
def recv_m2m_con_handle(data,m2m):
	# this function is is used to be callen if a m2m disconnects, we have to update all ws clients
	#rint("[A_m2m "+time.strftime("%H:%M:%S")+"] connection change")
	# client connected, generate challange
	if(data=="connect"):
		m2m.challange=get_challange()
	# client disconnected 
	elif(data=="disconnect"):
		p.rint2(str(m2m.mid)+"' disconneted","l","A_m2m")
		db.update_last_seen_m2m(m2m.mid,"") #<- this returns bad file descriptor

		# remove the m2m monitor from all its client lists (a_m2m)
		for a_m2m in server_m2m.clients:
			for viewer in a_m2m.m2m:
				if(viewer==m2m):
					p.rint("[A_m2m  "+time.strftime("%H:%M:%S")+"] releasing '"+str(a_m2m.mid)+"' from m2m monitor "+str(m2m.mid),"l")
					a_m2m.m2m.remove(viewer)
					
		# try to find that m2m in all ws clients lists, so go through all clients and their lists
		for ws in server_ws.clients:
			if(m2m in ws.v2m):
				p.rint2("releasing '"+str(ws.login)+"' from "+str(m2m.mid),"l","A_ws")
				ws.v2m.remove(m2m)
				msg={}
				msg["cmd"]="disconnect"
				msg["mid"]=m2m.mid
				msg["area"]=m2m.area
				msg["account"]=m2m.account
				msg["detection"]=m2m.detection
				msg_q_ws.append((msg,ws))
		try:
			server_m2m.clients.remove(m2m)
		except:
			pass
#******************************************************#
################## M2M CONNECTION #########################

################## M2M MESSAGE #########################
#******************************************************#
# recv_m2m_msg_q_handle will be called by the server structure shall just append
# the message to the queue
def recv_m2m_msg_q_handle(data,m2m):
	recv_m2m_msg_q.append((data,m2m))
#******************************************************#

#******************************************************#
# recv_m2m_msg_dq_handle will be called in the main loop and will forward a message
# to the recv_m2m_handle if there is one in the queue
def recv_m2m_msg_dq_handle():
	ret=0
	if(len(recv_m2m_msg_q)>0):
		ret=1
		recv_msg=recv_m2m_msg_q[0]
		recv_m2m_msg_q.remove(recv_msg)
		try:
			recv_m2m_msg_handle(recv_msg[0],recv_msg[1])
		except:
			p.err("sys:")
			p.err(str(sys.exc_info()[0]))
			p.err(str(sys.exc_info()[1]))
			p.err(str(repr(traceback.format_tb(sys.exc_info()[2]))))
	return ret
#******************************************************#

#******************************************************#
# this function, called by the dequeue above will handles all
# incoming messages and generate responses, which will be stored in the msg_q_m2m
def recv_m2m_msg_handle(data,m2m):
	global msg_q_m2m, msg_q_ws, rm, db, upload_dir
	# decode msg from string to dicc
	try:
		enc=json.loads(data)
	except:
		enc=""
		p.rint("-d--> json decoding failed","d")

	if(type(enc) is dict):
		# if the message would like to be debugged
		if(enc.get("debug",0)==1):
			for key, value in enc.items() :
				p.rint("-d-->Key:'"+str(key)+"' / Value:'"+str(value)+"'","d")

		# set last_comm token
		m2m.last_comm=time.time()

		#********* msg handling **************#
		# assuming that we could decode the message from json to dicc: we have to distingush between the commands:

		############## M2M CMD ############# block all but login commands, as long as we are not logged in
		if(m2m.logged_in==0 and enc.get("cmd")!="login" and enc.get("cmd")!="prelogin" and enc.get("cmd")!="register"):
			p.rint("[A_m2m "+time.strftime("%H:%M:%S")+"] A client tried to interact without beeing logged in","l")
			# send bad ack
			msg={}
			msg["cmd"]=enc.get("cmd")
			msg["ok"]=-2 # not logged in
			msg_q_m2m.append((msg,m2m))

		############## M2M CMD ############## pre login challange for M2M
		elif(enc.get("cmd")=="prelogin"):
			msg = {
				"cmd": 		enc.get("cmd"),
				"challange":	m2m.challange
			}
			msg_q_m2m.append((msg,m2m))
			#rint("received prelogin request, sending challange "+m2m.challange)

		############## M2M CMD ############# login try to set the logged_in to 1 to upload files etc, for M2M
		elif(enc.get("cmd")=="login"):
			msg={
				"cmd":	enc.get("cmd")
			}

			# get data, based on submitted MID
			db_r=db.get_data(enc.get("mid"))
			if(type(db_r) is int): #user not found
				try:
					ip=m2m.conn.getpeername()[0]
				except:
					ip="???"

				p.rint2("'"+str(enc.get("mid"))+"' ("+str(ip)+") not found in DB, log-in: failed","l","A_m2m",p.bcolors.WARNING)
				msg["ok"]=-3 # not logged in
			elif(db_r["area"]==""): #user not found
				p.rint2("'"+str(enc.get("mid"))+"' has a empty area='"+str(db_r["area"])+"', log-in: failed","l","A_m2m",p.bcolors.WARNING)
				msg["ok"]=-3 # not logged in
			else:
				h = hashlib.md5()
				h.update(str(db_r["pw"]+m2m.challange).encode("UTF-8"))
				# check parameter
				if(h.hexdigest()==enc.get("client_pw")):
					m2m.mid=enc.get("mid")
					# load all info from the db to the object in populate_m2m
					populate_m2m(m2m)	
					# this will set all the parameter for the m2m, makes sure that the rulemanager is loaded etc
					set_m2m_parameter(m2m,enc,db_r,msg)
					# disconenct all other sockets for this m2m
					for m2m_old in server_m2m.clients:
						if(m2m_old.mid==m2m.mid and m2m_old!=m2m):
							server_m2m.disconnect(m2m_old)
					# check if emergency updates are available
					if(db.e_update(m2m.mid)==1):
						p.rint2("Sending an emergency update request for m2m "+str(m2m.alias),"d","A_m2m",p.bcolors.WARNING)
						msg2 = { "cmd": "git_update" }
						msg_q_m2m.append((msg2,m2m))
				# special handling for monitor devices, we need to get on the m2m list of all our cameras
					if(m2m.m2m_monitor==1):
						for a_m2m in server_m2m.clients:
							if(a_m2m.account == m2m.account and a_m2m.area_id == m2m.area_id):
								p.rint("[A_m2m  "+time.strftime("%H:%M:%S")+"] adding '"+str(m2m.alias)+"' as monitor to the m2m list of "+str(a_m2m.alias),"l")
								a_m2m.m2m.append(m2m)
					else: # and if this is not the monitor, check if there is another monitor already online
						for a_m2m in server_m2m.clients:
							if(a_m2m.account == m2m.account and a_m2m.area_id == m2m.area_id and a_m2m.m2m_monitor==1):
								p.rint("[A_m2m  "+time.strftime("%H:%M:%S")+"] adding '"+str(a_m2m.alias)+"' as monitor to the m2m list of "+str(m2m.alias),"l")
								m2m.m2m.append(a_m2m)
				# bad password
				else:
					p.rint2("'"+str(enc.get('mid'))+"' log-in: failed","l","A_m2m",p.bcolors.FAIL)
					msg["ok"]=-2 # not logged in
			# send message in any case
			msg_q_m2m.append((msg,m2m))

		############## M2M CMD ############# heartbeat, for M2M
		elif(enc.get("cmd")=="m2m_hb"):
			# respond
			p.rint2("'"+str(m2m.mid)+"' / '"+str(m2m.alias)+"' HB updating "+str(len(m2m.m2v))+" clients","h","A_m2m")
			msg = {
				"mid":	m2m.mid,
				"cmd":	enc.get("cmd"),
				"ok":	1
			}
			msg_q_m2m.append((msg,m2m))

			# tell subscribers
			msg={
				"mid":	m2m.mid,
				"cmd":	enc.get("cmd"),
				"ts":	time.time()
			}
			for subscriber in m2m.m2v:
				#rint("Tell that to "+subscriber.login)
				msg_q_ws.append((msg,subscriber))


		############## M2M CMD ############# confirm that is changed the state of detection or tell us that there is movement, for M2M
		elif(enc.get("cmd")=="state_change"):
			m2m.state=enc.get("state",4)
			m2m.detection=enc.get("detection",-1)

			# prepare notification system, arm or disarm
			if(m2m.state==1 and m2m.detection>=1): # state=1 means Alert!
				#start_new_alert for this m2m
				m2m.alert.notification_send=0 # the loop searchs for alerts, with notification_send==0
				m2m.alert.notification_send_ts = -1 # not used
				m2m.alert.collecting=1	# means activly collecting
				m2m.alert.id=db.create_alert(m2m,rm.get_account(m2m.account).get_area(m2m.area).print_rules(bars=0,account_info=0,print_out=0))
				m2m.alert.ts=time.time()
				m2m.alert.files = []	# file names
				m2m.alert.last_upload=0
				
			elif(m2m.detection==0): 
				# assuming that the system was already triggered and right after that the switch off command arrived -> avoid notification
				# check_alerts will search for m2m with notification_send==0
				m2m.alert.notification_send = 1 # indicate that this is done

			# prepare messages
			# tell subscribers
			msg={
				"mid":		m2m.mid,
				"cmd":		enc.get("cmd"),
				"alarm_ws":	m2m.alarm_ws,
				"state":	m2m.state,
				"area":		m2m.area,
				"account":	m2m.account,
				"detection":	m2m.detection,
				"rm":		rm.get_account(m2m.account).get_area(m2m.area).print_rules(bars=0,account_info=0,print_out=0)
			}
			informed=0
			for subscriber in m2m.m2m:
				msg_q_m2m.append((msg,subscriber))
			for subscriber in m2m.m2v:
				msg_q_ws.append((msg,subscriber))
				informed+=1

			# print on console
			p.change_state(m2m,informed)


		############## M2M CMD ############# wf -> write file, message shall send the fn -> filename and set the EOF -> 1 if it is the last piece of the file , for M2M
		elif(enc.get("cmd")=="wf"):
			# handle new file
			if(m2m.openfile!=enc.get("fn")):
				if(m2m.fp!=""):
					try:
						m2m.fp.close()
					except:
						m2m.fp=""
				m2m.openfile = enc.get("fn")
				base_location=str(int(time.time()))+"_"+m2m.mid+"_"+m2m.openfile
				des_location=os.path.join(upload_dir,base_location)
				m2m.fp = open(des_location,'wb')
				# this is the start of a transmission
				# a client in ALERT state will send UP TO N pictures, but might be disconnected before he finished.
				# we'll put every alert file filename in the m2m.alert_img list  and check in the loop if that list
				# has reached 5 pics, or hasn't been updated for > 20 sec.
				# if those conditions are satifies we'll check if the mail optioin is active and if so mail it to
				# the given address. after that we set the m2m.alert_mail_send to 1 state change to low should clear that
				if(m2m.state==1 and m2m.detection>=1): # ALERT
					# not yet send, append fn to list and save timestamp  || OR || detection = permanant fire, then we're going to save the picture, even after fireing the mail
					if( (m2m.alert.collecting==1 and m2m.alert.notification_send==0) or (m2m.detection==2)): 
						db.append_alert_photo(m2m,base_location)
						m2m.alert.files.append(des_location)
						m2m.alert.last_upload = time.time()
				m2m.paket_count_per_file=0

			# write this file
			m2m.fp.write(base64.b64decode(enc.get("data").encode('UTF-8')))

			# check if this packet contained the end of file
			if(enc.get("eof")==1):
				# end of file, close it
				this_file=m2m.fp.name  #store the name just in case we have to read it again
				try:
					m2m.fp.close()
				except:
					m2m.fp=""
				m2m.openfile=""	

				#debugging
				debug_in.update(m2m.mid)

				# prepare client message
				msg={
					"mid":		m2m.mid,
					"cmd":		"rf",
					"state":	m2m.state,
					"area":		m2m.area,
					"detection":	m2m.detection,
					"up_down_debug":debug_in.get(m2m.mid)+" || "+debug_out.get(m2m.mid)
				}
				# all image data in one packet
				if(enc.get("sof",0)==1):
					#send img, assuming this is a at once img
					msg["img"]=enc.get("data")
					try:
						msg["ts"]=(enc.get("td"),0)[0][0]
					except:
						msg["ts"]=0
				else:
					#read img and send at once, close this file pointer as it is writing only
					try:
						m2m.fp = open(this_file,'rb')
						msg["img"]=m2m.fp.read()
						m2m.fp.close()
					except:
						m2m.fp=""
					
				# select the ws to send to
				if(m2m.state==1 and m2m.detection>=1): # alert -> inform everyone
					if(m2m.alarm_ws == 1): # only forward the image to the ws whenever this camera has alarm_ws set
						# the m2v list has all viewer
						for v in m2m.m2v:
							if(v.snd_q_len<10 and v.alarm_view==1): # just send it if their queue is not to full AND the clients wants unrequested img
								msg_q_ws.append((msg,v))
								v.snd_q_len+=1	
				# webcam -> use webcam list as the m2v list has all viewer, but the webcam has those who have requested the feed
				for v in m2m.webcam:
					#only update if last ts war more then interval ago
					ts_photo=enc.get("td",0) # td tells us when this photo was taken
					ts_photo=ts_photo[1][0]
					t_passed=ts_photo-v.ts+0.1
					if(t_passed>=v.interval and v.ws.snd_q_len<1 and v.ws.webcam_countdown>=1): # send only if queue is not too full
						v.ts=ts_photo
						v.ws.snd_q_len+=1
						v.ws.webcam_countdown-=1
						msg["webcam_countdown"]=v.ws.webcam_countdown
						msg_q_ws.append((msg,v.ws))
					elif(v.ws.webcam_countdown<1):
						p.rint("countdows 1","u")
						set_webcam_con(m2m.mid,0,v.ws) # disconnect the webcam from us						
					else:
						p.rint("skipping "+str(v.ws.login)+": "+str(t_passed)+" / "+str(v.ws.snd_q_len),"u")

				#  delete the picture from our memory, as it can not be a alert picture
				if(m2m.detection==0):
					os.remove(this_file)

				tmp_loc=this_file.split('/') # short filename
				p.rint2("'"+str(m2m.mid)[-5:]+"'/'"+str(m2m.alias)+"'@'"+str(m2m.account)+"' uploaded "+(tmp_loc[len(tmp_loc)-1])[-15:],"u","A_m2m",p.bcolors.GREY)
			m2m.paket_count_per_file+=1

		############## M2M CMD ############# register a new m2m device ####
		elif(enc.get("cmd")=="register"):
			#rint("register request received")
			db_r=db.get_ws_data(enc.get("login",""))
			msg={}
			msg["cmd"]=enc.get("cmd")

			if(type(db_r) is int): #user not found results in return -1 or so
				#rint("db error")
				p.rint2("Register: '"+str(enc.get("login","no_login"))+"' not found in DB, log-in: failed","l","A_ws")
				msg["ok"]=-3 # not logged in
			else:
				p.rint2("'"+str(enc.get("login","no_login"))+"' logged in, registering a new device","l","A_ws")
				# generate hash for DB passwort and challange, client will generate hash_of(hash_of(passwort) + challange)
				# this enables us to save only the hash_of(password) instead of clear text password in the db!
				h = hashlib.md5()
				hashme=(str(db_r["pw"])+m2m.challange).encode("UTF-8")
				h.update(hashme)
				#rint("check pw")
				#rint("for secure login we have: pw="+str(db_r["pw"])+" challange="+str(m2m.challange)+" together="+str(hashme)+" hash="+str(h.hexdigest())+" vs received="+enc.get("password"))

				# check parameter
				if(h.hexdigest()==enc.get("password") and db_r["account"]!=""):
					db_r2=db.register_m2m(enc.get("mid"),enc.get("m2m_pw"),db_r["account"],enc.get("alias","SecretCam"))
					# complete message
					if(db_r2==0):
						msg["ok"]=1 # logged in
					else:
						msg["ok"]=-1 # db error
				else:
					msg["ok"]=-2 # wrong pw

			# respode
			msg_q_m2m.append((msg,m2m))

		############## M2M CMD ############# response on the previously send git update command ####
		elif(enc.get("cmd")=="git_update"):
			r = enc.get("cmd_result","no cmd_result")
			if(r.find("up-to-date")==-1 and r.find("Updating")==-1 and r.find("Aktualisiere")==-1):
				ignore = 1  # 2do, analyse if that was a success
				p.err("git update response: "+r)
			msg={"cmd":"reboot"}
			msg_q_m2m.append((msg,m2m))
		
		############## M2M CMD ############# send the camera to reboot after the alias was changed successful ###
		elif(enc.get("cmd")=="set_alias"):
			if(str(enc.get("ok","-1"))=="1"):
				msg={"cmd":"reboot"}
				msg_q_m2m.append((msg,m2m))
				p.rint2("M2M "+m2m.alias+" has updated its name, rebooting it","d","A_ws")
			else:
				p.rint2("M2M "+m2m.alias+" update alias failed!","d","A_ws")

		############## M2M CMD ############# response from the m2m that the external pin was toggled,forward it ##
		elif(enc.get("cmd")=="toggle_external_pin"):
			msg={}
			msg["cmd"]=enc.get("cmd")
			msg["mid"]=m2m.mid
			msg["ok"]=0
			msg["state"]=m2m.external_state

			p.rint2("M2M "+m2m.alias+" responds to pin update.","d","A")

			# send a message to al listeners
			for l in m2m.m2v:
				msg_q_ws.append((msg,l))
		############## M2M CMD ############# unsupported command, for M2M
		else:
			p.rint("unsupported m2m command: "+str(enc.get("cmd")),"d")

		############### CONNECTION ##### send good ack
		if(enc.get("ack",0)!=0):
			msg={
				"cmd":		enc.get("cmd"),
				"ack_ok":	1
			}
			msg_q_m2m.append((msg,m2m))
		############### CONNECTION ##### send good ack


		#********* msg handling **************#
	#### comm error , for M2M
	else:
		p.err("JSON decode on m2m message error!")
		msg={}
		#msg["cmd"]=enc.get("cmd")
		msg["ok"]=-1 #comm error
		msg_q_m2m.append((msg,m2m))
#******************************************************#

#******************************************************#
# last but not least we have to send messages to the M2M
# this is done by the snd_m2m_msg_dq_handle. it will check if there
# is a message and forward it to the server
def snd_m2m_msg_dq_handle():
	ret=0
	if(len(msg_q_m2m)>0):
		ret=1
		#rint(str(time.time())+' fire in the hole')
		data=msg_q_m2m[0]
		msg_q_m2m.remove(data)
		#rint(data)

		msg=data[0]
		m2m=data[1]
		if(0!=server_m2m.send_data(m2m,json.dumps(msg).encode("UTF-8"))):
			# the cam box m2m unit is not longer available .. obviously, remove it from every viewer and inform them
			recv_m2m_con_handle("disconnect",m2m)
	return ret
#******************************************************#
#***************************************************************************************#
#************************************** end of m2m *************************************#
#***************************************************************************************#


#***************************************************************************************#
#************************************** WebSockets *************************************#
#***************************************************************************************#
# introduction text required

#******************************************************#
# the websocket server will call this funcion to put new incoming connection in the queue
def recv_ws_con_q_handle(data,ws):
	recv_ws_con_q.append((data,ws))
#******************************************************#

#******************************************************#
# the main loop will call the dequeue to check if there are new connection changes
def recv_ws_con_dq_handle():
	ret=0
	if(len(recv_ws_con_q)>0):
		ret=1
		recv_con=recv_ws_con_q[0]
		recv_ws_con_q.remove(recv_con)
		try:
			recv_ws_con_handle(recv_con[0],recv_con[1])
		except:
			p.err("sys:")
			p.err(str(sys.exc_info()[0]))
			p.err(str(sys.exc_info()[1]))
			p.err(str(repr(traceback.format_tb(sys.exc_info()[2]))))

	return ret
#******************************************************#

#******************************************************#
# dequeue above will call us to process the new connection
def recv_ws_con_handle(data,ws):
	# this function is is used to be callen if a ws disconnects, we have to update all m2m clients and their webcam lists
	#rint("[A_ws "+time.strftime("%H:%M:%S")+"] connection change")
	if(data=="disconnect"):
		try:
			ip=ws.ip
			p.rint2("WS "+str(ip)+"/"+str(ws.login)+" disconneted","l","A_ws")
		except:
			p.rint2("WS disconneted","l","A_ws")
		# try to find that websockets in all client lists, so go through all clients and their lists
		for m2m in server_m2m.clients:
			for viewer in m2m.m2v:
				if(viewer==ws):
					p.rint("[A_ws  "+time.strftime("%H:%M:%S")+"] releasing '"+str(m2m.mid)+"' from "+str(ws.login),"l")
					m2m.m2v.remove(viewer)

					# also check if that ws has been one of the watchers of the webfeed
					set_webcam_con(m2m.mid,0,ws)
		try:
			server_ws.clients.remove(ws)
		except:
			ignore=1
	elif(data=="connect"):
		ws.challange=get_challange()
#******************************************************#

#******************************************************#
# websocket server will call this function if new websocket messages arrived
# we will store them in the queue
def recv_ws_msg_q_handle(data,ws):
	recv_ws_msg_q.append((data,ws));
#******************************************************#

#******************************************************#
# and the main loop will call dequeue them if there are any and call msg_handle
def recv_ws_msg_dq_handle():
	ret=0
	if(len(recv_ws_msg_q)>0):
		ret=1
		recv_msg=recv_ws_msg_q[0]
		recv_ws_msg_q.remove(recv_msg)
		try:
			recv_ws_msg_handle(recv_msg[0],recv_msg[1])
		except:
			p.err("sys:")
			p.err(str(sys.exc_info()[0]))
			p.err(str(sys.exc_info()[1]))
			p.err(str(repr(traceback.format_tb(sys.exc_info()[2]))))
	return ret
#******************************************************#

#******************************************************#
# callen by the dequeue above
def recv_ws_msg_handle(data,ws):
	global db, upload_dir
	try:
		enc=json.loads(data)
	except:
		enc=""
		p.rint("-d--> json decoding failed on:" + str(data),"d")

		#rint("ws:"+str(ws.port)+"/"+str(ws.ip))
	if(type(enc) is dict):
		ws.last_comm=time.time()

		if(enc.get("debug",0)==1):
			p.rint("websocket_msg","d")
			for key, value in enc.items() :
				p.rint("-d-->Key:'"+key+"' / Value:'"+str(value)+"'","d")


		############## WS CMD ############# pre login challange, for WS
		if(enc.get("cmd")=="prelogin"):
			handle_ws_prelogin(enc,ws)
		############## WS CMD ############# LOGIN from a viewer, for WS
		elif(enc.get("cmd")=="login"):
			handle_ws_login(enc,ws)

		############## WS CMD ############# refresh, this will be called by the app, on a start. 
		# the service is already connected to us, so no need for a complete reconnect, but the app will need an update about all clients
		elif(enc.get("cmd")=="refresh_ws"):
			handle_ws_refresh_ws(enc,ws)

		############## WS CMD ############# heartbeat, for WS
		elif(enc.get("cmd")=="ws_hb"):
			handle_ws_hb(enc,ws)

		############## WS CMD ############# reset webcam_countdown
		elif(enc.get("cmd")=="reset_webcam_countdown"):
			handle_ws_reset_countdown(enc,ws)

		############## WS CMD ############# get all areas for account
		elif(enc.get("cmd")=="get_areas"):
			handle_ws_get_areas(enc,ws)
		############## WS CMD ############# set updated area parameter
		elif(enc.get("cmd")=="update_area"):
			handle_ws_update_area(enc,ws)
		############## WS CMD ############# remove an area
		elif(enc.get("cmd")=="remove_area"):
			handle_ws_remove_area(enc,ws)

		############## WS CMD ############# set updated camera parameter
		elif(enc.get("cmd")=="update_cam_parameter"):
			handle_update_cam_parameter(enc,ws)
		############## WS CMD ############# get all cams for account
		elif(enc.get("cmd")=="get_cams"):
			handle_ws_get_cams(enc,ws)
		############## WS CMD ############# remove an m2m
		elif(enc.get("cmd")=="remove_m2m"):
			handle_ws_remove_m2m(enc,ws)

		############## WS CMD ############# get all logins for account
		elif(enc.get("cmd")=="get_logins"):
			handle_ws_get_logins(enc,ws)
		############## WS CMD ############# register new ws login
		elif(enc.get("cmd")=="new_register"):
			handle_ws_new_register(enc,ws)
		############## WS CMD ############# update a login
		elif(enc.get("cmd")=="update_login"):
			handle_ws_update_login(enc,ws)
		############## WS CMD ############# remove a login
		elif(enc.get("cmd")=="remove_login"):
			handle_ws_remove_login(enc,ws)

		############## WS CMD ############# get IDs of open alerts
		elif(enc.get("cmd")=="get_alert_ids"):
			handle_ws_get_alert_ids(enc,ws)
		############## WS CMD ############# get Details to a alarm ID
		elif(enc.get("cmd")=="get_alarm_details"):
			handle_ws_get_alert_details(enc,ws)
		############## WS CMD ############# send pictures to email adress
		elif(enc.get("cmd")=="send_alert"):
			handle_ws_send_alert(enc,ws)
		############## WS CMD ############# acknowledge alerts
		elif(enc.get("cmd")=="ack_alert" or enc.get("cmd")=="ack_all_alert" or enc.get("cmd")=="del_alert"):
			handle_ws_ack_del_alert(enc,ws)
		############## WS CMD ############# get picture, used by the alarms
		elif(enc.get("cmd")=="get_img"):
			handle_ws_get_image(enc,ws)

		############## WS CMD ############# Detection on/off handle, for WS 
		# --> this should be only an backup as the server can decide on its own when to activate the detection
		elif(enc.get("cmd")=="set_override"):
			handle_ws_set_override(enc,ws)
		############## WS CMD ############# if a ws client supports location grabbing it can send location updates to switch on/off the detection, for WS
		elif(enc.get("cmd")=="update_location"):
			handle_ws_update_location(enc,ws)
		############## WS CMD ############# get all rules for acocunt
		elif(enc.get("cmd")=="get_rules"):
			handle_ws_get_rules(enc,ws)
		############## WS CMD ############# modify geo rule
		elif(enc.get("cmd")=="update_rule_geo"):
			handle_ws_update_rule_geo(enc,ws)

		############## WS CMD ############# send a update command to the m2m
		elif(enc.get("cmd")=="git_update"):
			handle_ws_git_update(enc,ws)
		############## WS CMD ############# set a color 
		elif(enc.get("cmd")=="set_color"):
			handle_ws_set_color(enc,ws)
		############## WS CMD ############# webcam interval -> sign in or out to webcam, for WS
		elif(enc.get("cmd")=="set_interval"):
			set_webcam_con(enc.get("mid"), enc.get("interval",0) ,ws)
		############## WS CMD ############# set the state of the external pin WS
		elif(enc.get("cmd")=="toggle_external_pin"):
			handle_ws_toggle_external_pin(enc,ws)

		############## WS CMD ############# unsupported cmd, for WS
		else:
			p.rint("[A ws  "+time.strftime("%H:%M:%S")+"] unsupported ws command: "+enc.get("cmd")+ " from "+str(ws.login),"d")
#******************************************************#
#******************************************************#
# and here again: the main loop will call us to check if there is a message to send back
def snd_ws_msg_dq_handle():
	ret=0
	if(len(msg_q_ws)>0):
		ret=1
		#rint(str(time.time())+' fire in the hole')
		data=msg_q_ws[0]
		msg=data[0]
		cli=data[1]
		#try to submit the data to the websocket client, if that fails, remove that client.. and maybe tell him
		msg_q_ws.remove(data)

		cli.snd_q_len=max(0,cli.snd_q_len-1)
		if(server_ws.send_data(cli,json.dumps(msg).encode("UTF-8"))!=0):
			recv_ws_con_handle("disconnect",cli)

		#debugging
		if(msg.get("cmd",0)=="rf"):
			debug_out.update(msg.get("mid",0))
			#rint(debug_in.print(msg.get("mid",0)))
			#rint(debug_out.print(msg.get("mid",0)))
	return ret
#******************************************************#
########### HANDLE ws update location ################
def handle_ws_update_location(enc,ws):
	ws.location=enc.get("loc","")
	p.rint("[A_ws  "+time.strftime("%H:%M:%S")+"] '"+str(ws.login)+"'@'"+str(ws.account)+"' moved to '"+enc.get("loc")+"'","h")
	# step 1: update database location for this login
	db_r=db.update_location(ws.login,ws.location)
	# step 2: run all rule checks and update every box on the account
	t=time.time()
	p.rint("[A_RM  "+time.strftime("%H:%M:%S")+"] checking as somebody moved for this account","r")
	rm_check_rules(ws.account,ws.login,1)	# check and use db
	p.rint("[A_RM  "+time.strftime("%H:%M:%S")+"] Check took "+str(time.time()-t),"r")
########### HANDLE ws update location ################

########### HANDLE ws remove area ################
def handle_ws_remove_area(enc,ws):
	msg={
		"cmd":	enc.get("cmd"),
		"ok":	db.remove_area(enc.get("id")),
	}
	msg_q_ws.append((msg,ws))						
	p.rint("[A_ws  "+time.strftime("%H:%M:%S")+"] '"+str(ws.login)+"' deleted area  '"+str(enc.get("id"))+"'","d")
########### HANDLE ws remove area ################

########### HANDLE ws remove m2m ################
def handle_ws_remove_m2m(enc,ws):
	mid=enc.get("mid")
	msg={
		"cmd":	enc.get("cmd"),
		"ok":	db.remove_m2m(mid,ws.account),
		"mid":	mid
	}
	# send a message to every client of the server account
	for v in server_ws.clients:
		if(v.account == ws.account):
			msg_q_ws.append((msg,v))						
	p.rint("[A_ws  "+time.strftime("%H:%M:%S")+"] '"+str(ws.login)+"' deleted m2m '"+str(mid)+"'","d")
########### HANDLE ws remove m2m ################

########### HANDLE ws update login ################
def handle_ws_update_login(enc,ws):
	msg={
		"cmd":	enc.get("cmd"),
		"id":	enc.get("id"),
		"ok":	db.update_login(enc.get("id"), enc.get("name"), enc.get("pw"), enc.get("email"), ws.account)
	}
	msg_q_ws.append((msg,ws))						
	p.rint("[A_ws  "+time.strftime("%H:%M:%S")+"] '"+str(ws.login)+"' updated login  '"+str(enc.get("id"))+"'","d")
########### HANDLE ws update login ################

########### HANDLE ws remove login ################
def handle_ws_remove_login(enc,ws):
	msg={
		"cmd":	enc.get("cmd"),
		"ok":	db.remove_login(enc.get("id"),ws.account)
	}
	msg_q_ws.append((msg,ws))						
	p.rint("[A_ws  "+time.strftime("%H:%M:%S")+"] '"+str(ws.login)+"' deleted login  '"+str(enc.get("id"))+"'","d")
########### HANDLE ws remove login ################

########### HANDLE ws get alarm id's ################
def handle_ws_get_alert_ids(enc,ws):
	mid=enc.get("mid")
	open_start=enc.get("open_start",0)
	open_end=enc.get("open_end",10)
	closed_start=enc.get("closed_start",0)
	closed_end=enc.get("closed_end",10)

	msg={
		"cmd": 		enc.get("cmd"),
		"ids_open":	[],
		"ids_closed":	[],
		"mid":		mid,
		"open_max":	db.get_open_alert_count(ws.account, mid),
		"closed_max":	db.get_closed_alert_count(ws.account, mid)
	}

	db_r=db.get_open_alert_ids(ws.account,mid,open_start,open_end)
	if(db_r==-1):
		msg["ids_open"].append(-1)
	else:
		for i in db_r:
			msg["ids_open"].append(i['id'])

	db_r=db.get_closed_alert_ids(ws.account,mid,closed_start,closed_end)
	if(db_r==-1):
		msg["ids_closed"].append(-1)
	else:
		for i in db_r:
			msg["ids_closed"].append(i['id'])
	msg_q_ws.append((msg,ws))
########### HANDLE ws get alarm id's ################

########### HANDLE ws get alarm details ################
def handle_ws_get_alert_details(enc,ws):
	id=enc.get("id")
	if(id!=-1):
		p.rint("[A_WS  "+time.strftime("%H:%M:%S")+"] Received request for alarm: "+str(id)+" details","v")
		db_r1=db.get_alert_details(ws.account,id)
		## get number of pictures for this alert
		db_r2=db.get_img_count_for_alerts(id)
		## get picture path for 0..100 
		db_r3=db.get_img_for_alerts(id,0)
		msg={
			"cmd":		enc.get("cmd"),
			"id":		id,
			"rm_string":	db_r1['rm_string'],
			"f_ts":		db_r1['f_ts'],
			"img_count":	db_r2,
			"img":		db_r3,
			"mid":		enc.get("mid"),
			"ack":		db_r1['ack'],
			"ack_ts":	db_r1['ack_ts'],
			"ack_by":	db_r1['ack_by']
		}
		msg_q_ws.append((msg,ws))
########### HANDLE ws get alarm details ################

########### HANDLE ws get send alert ################
def handle_ws_send_alert(enc,ws):
	id=enc.get("aid")

	msg={}
	msg["cmd"]=enc.get("cmd")
	msg["mid"]=enc.get("mid")
	msg["aid"]=id
	msg["status"]=-1

	p.rint2("Received request for alarm: "+str(id)+" to mail","v","A_ws")
	if(id!=-1 and ws.email!=""):
		## get picture path for 0..100 
		db_r3=db.get_img_for_alerts(id,0)
		db_account=db.get_account_for_path(db_r3[0]['path'])
		if(str(db_account)==str(ws.account)):
			file_lst=[]
			for f in db_r3:
				file_lst.append(os.path.join(upload_dir,f['path']))
			send_mail.send( "Request pictures for alarm "+str(id), "Bittesehr", files=file_lst, send_to=ws.email,send_from="koljasspam493@gmail.com", server="localhost")		
		msg["status"]=1
	else:
		p.rint("[A_WS  "+time.strftime("%H:%M:%S")+"] Invalid request for alarm: "+str(id)+" to mail "+str(ws.email),"v")
	msg_q_ws.append((msg,ws))
########### HANDLE ws get send alert ################

########### HANDLE ws reset countdown ################
def handle_ws_reset_countdown(enc,ws):
	ws.webcam_countdown=99
	p.rint("[A_WS  "+time.strftime("%H:%M:%S")+"] Received countdown reset from "+str(ws.login),"v") 
########### HANDLE ws reset countdown ################

########### HANDLE ws get image ################
def handle_ws_get_image(enc,ws):
	path=enc.get("path")
	p.rint("[A_WS  "+time.strftime("%H:%M:%S")+"] Received request for img: "+str(path)+", sending","v")
	if(db.get_account_for_path(path)==ws.account):
		msg={}
		msg["cmd"]="recv_req_file"
		msg["path"]=path
		msg["height"]=enc.get("height")
		msg["width"]=enc.get("width")

		try:
			img = open(os.path.join(upload_dir,path),'rb')
		except:
			img = open("../webserver/images/filenotfound.jpg",'rb')
			
		strng=img.read(512000-100)
		img.close()
		msg["img"]=base64.b64encode(strng).decode('utf-8')
		msg_q_ws.append((msg,ws))
########### HANDLE ws get image ################

########### HANDLE ws ack / ack all / del alert ################
def handle_ws_ack_del_alert(enc,ws):
	if(enc.get("cmd")=="ack_alert"):
		db.ack_alert(enc.get("mid"),enc.get("aid"),ws.login)
	elif(enc.get("cmd")=="ack_all_alert"):
		db.ack_all_alert(enc.get("mid"),ws.login)
	elif(enc.get("cmd")=="del_alert"):
		db.del_alert(enc.get("mid"),enc.get("aid"),ws.login)
	msg={}
	msg["cmd"]="update_open_alerts"
	msg["mid"]=enc.get("mid")
	msg["open_alarms"]=db.get_open_alert_count(ws.account,enc.get("mid"))
	for cam in server_m2m.clients:
		if(cam.mid==enc.get("mid")):
			for v in cam.m2v:
				msg_q_ws.append((msg,v))
			break
########### HANDLE ws ack / ack all / del alert ################

########### HANDLE ws register a new login ################
def handle_ws_new_register(enc,ws):
	res=db.register_ws(enc.get("user"),enc.get("pw"),enc.get("email"))		
	msg={}
	msg["cmd"]=enc.get("cmd")
	msg["status"]=res
	msg_q_ws.append((msg,ws))
	p.rint("[A ws  "+time.strftime("%H:%M:%S")+"] new register from "+str(enc.get("user")),"d")
########### HANDLE ws register a new login ################

########### HANDLE ws get area data ################
def handle_ws_get_areas(enc,ws):
	msg={}
	msg["cmd"]=enc.get("cmd")
	areas=db.get_areas_for_account(ws.account)
	if(areas!=-1):
		msg["ok"]=1
		msg["areas"]=[]#areas
		for a in areas:
			msg["areas"].append((a))
	else:
		msg["ok"]=-1
	msg_q_ws.append((msg,ws))
########### HANDLE ws get area data ################

########### HANDLE ws get rules ################
def handle_ws_get_rules(enc,ws):
	msg={}
	msg["cmd"]=enc.get("cmd")
	msg["rules"]=rm.get_account(ws.account).print_account(m_dict=1)
	msg_q_ws.append((msg,ws))
########### HANDLE ws get rules ################

########### HANDLE ws get logins ################
def handle_ws_get_logins(enc,ws):
	msg={}
	msg["cmd"]=enc.get("cmd")
	all_logins4account=db.get_logins4account(ws.account)
	if(type(all_logins4account) is int):
		p.rint("Error getting login-data for account "+str(ws.account),"d")
		msg["ok"]=-1
	else:
		msg["ok"]=1
		msg["m2m"]=all_logins4account
	msg_q_ws.append((msg,ws))
########### HANDLE ws get logins ################

########### HANDLE ws hb ################
def handle_ws_hb(enc,ws):
	# respond
	p.rint("[A_ws  "+time.strftime("%H:%M:%S")+"] '"+str(ws.login)+"'@'"+str(ws.account)+"' HB","h")
	msg={}
	msg["cmd"]=enc.get("cmd")
	msg["ok"]=1
	msg_q_ws.append((msg,ws))
########### HANDLE ws hb ################

########### HANDLE ws update area ################
def handle_ws_update_area(enc,ws):
	area_id=enc.get("id","0")
	area_old_name=""
	# 1. get old name of area id
	areas=db.get_areas_for_account(ws.account)
	for a in areas:
		if(str(a["id"])==str(area_id)):
			area_old_name=a["area"]
			break

	# 2. save new parameter
	for m2m in server_m2m.clients:
		if(m2m.area==area_old_name):
			# it might sound rough, but we have to reloader all the rules, 
			# change the area, inform all clients. If we just disconnect to box
			# it will redial in in 3 sec and everything will run on its own
			server_m2m.disconnect(m2m)

	# 3. update database
	db.update_area(enc.get("id",0), enc.get("name",""), enc.get("latitude","0.0"), enc.get("longitude","0.0"), ws.account)

	# 4. send ok response to ws
	msg={}
	msg["cmd"]=enc.get("cmd")
	msg["ok"]=1
	msg["id"]=enc.get("id");
	msg["name"]=enc.get("name");
	msg["latitude"]=enc.get("latitude");
	msg["longitude"]=enc.get("longitude");

	msg_q_ws.append((msg,ws))
	p.rint("[A_ws  "+time.strftime("%H:%M:%S")+"] '"+str(ws.login)+"' updated area parameter for '"+enc.get("name","")+"'","d")
########### HANDLE ws update area ################			


########### HANDLE ws get cams ################
def handle_ws_get_cams(enc,ws):
	msg={}
	msg["cmd"]=enc.get("cmd")
	msg["v_short"]=str(subprocess.Popen(["git","-C", os.path.dirname(os.path.realpath(__file__)), "rev-list", "HEAD", "--count"],stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.PIPE).communicate()[0].decode()).replace("\n","")
	msg["v_hash"]=str(subprocess.Popen(["git","log", "--pretty=format:%h", "-n", "1"],stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.PIPE).communicate()[0].decode())

	all_m2m4account=db.get_m2m4account(ws.account)
	# if it failed
	if(type(all_m2m4account) is int):
		p.rint("Error getting data for account "+str(ws.account),"d")
		msg["ok"]=-1
	else:
		# is cam online
		for m2m in all_m2m4account:
			m2m["online"]=0
			for cli in server_m2m.clients:
				if(str(cli.mid) == str(m2m["mid"])):
					m2m["online"]=1
					break

		msg["ok"]=1
		msg["m2m"]=all_m2m4account
	msg_q_ws.append((msg,ws))
########### HANDLE ws get cams ################

########### HANDLE ws update rule geo ################
def handle_ws_update_rule_geo(enc,ws):
	t_area=enc.get("name","")
	msg={}
	msg["ok"]=-1

	# 3. no go, get it
	rules=rm.get_account(ws.account).get_area(t_area).rules

	# 4. check if geo rules exists
	id=-1
	for rule in rules:
		if(rule.conn=="nobody_at_my_geo_area"):
			id=rule.id
			break
	
	# 5.1. add rule, only if not existing
	if(str(enc.get("geo","0"))==str("1") and id==-1):
		rm.get_account(ws.account).get_area(t_area).append_rule("nobody_at_my_geo_area",0,0)
		msg["ok"]=1
	# 5.2. remove rule, only if existing
	elif(str(enc.get("geo","1"))==str("0") and id!=-1):
		rm.get_account(ws.account).get_area(t_area).rm_rule(id)
		msg["ok"]=1

	# 6. send response
	msg["cmd"]=enc.get("cmd")
	msg_q_ws.append((msg,ws))

	# 7. resend rules to refresh user dash
	msg={}
	msg["cmd"]="get_rules"
	msg["rules"]=rm.get_account(ws.account).print_account(m_dict=1)
	msg_q_ws.append((msg,ws))

	# 8. rescan all rules to update booble
	rm_check_rules(ws.account,ws.login,0)
########### HANDLE ws update rule geo ################

########### HANDLE ws git update ################
def handle_ws_git_update(enc,ws):
	p.rint2("Received request to update mid:"+str(enc.get("mid","-")),"d","A ws")
	msg={}
	msg["cmd"]=enc.get("cmd")
	msg["ok"]=-1

	for cam in server_m2m.clients:
		if(enc.get("mid")==cam.mid and cam.account==ws.account):
			msg["ok"]=0
			# send msg to m2m
			msg2={}
			msg2["cmd"]=enc.get("cmd")
			msg_q_m2m.append((msg2,cam))
			break

	# send msg back to ws
	msg_q_ws.append((msg,ws))
########### HANDLE ws git update ################

########### HANDLE ws toggle pin ################
def handle_ws_toggle_external_pin(enc,ws):
	# search the right cam, based on the MID and account
	for cam in server_m2m.clients:
		if(enc.get("mid")==cam.mid and cam.account==ws.account):
			cam.external_state=int(not(cam.external_state))

			# send msg to m2m
			msg2={}
			msg2["cmd"]=enc.get("cmd")
			msg2["state"]=cam.external_state
			msg_q_m2m.append((msg2,cam))

			db.update_external_state(enc.get("mid"),cam.external_state)
	p.rint2("User '"+str(ws.login)+"' asked to toggle the pin '"+str(enc.get("mid"))+"'","d","A ws")
########### HANDLE ws toggle pin ################

########### HANDLE ws set override ws ################
def handle_ws_set_override(enc,ws):
	area=enc.get("area")
	rule=enc.get("rule") # can be "*" for on or "/" for off
	duration=int(enc.get("duration"))
	p.rint("[A_ws  "+time.strftime("%H:%M:%S")+"] '"+str(ws.login)+"' sets a override '"+str(rule)+"' for area '"+str(area)+"'","v")
			
	# prepare msg
	msg={}
	msg["cmd"]=enc.get("cmd")

	r=rm.get_account(ws.account)
	if(r!=0):
		rule_area=r.get_area(area)
		if(rule_area!=0):
			# check if opposit rule existed and remove up front
			p.rint("[A_ws  "+time.strftime("%H:%M:%S")+"] RM, remove override rule","r")
			rule_area.rm_override("/")
			rule_area.rm_override("*")
					
			if(rule!=""): # add new override as no one was in place to remove
				if(duration>0): # duration can be a time in sec or -1 = forever
					duration=int(time.time()+duration)
				rule_area.append_rule(rule,duration,0)	
				
			msg["status"]="running rule check"
			msg["ok"]=1
			msg["rm_override"]=""
			if(rule_area.has_override_detection_off):
				 msg["rm_override"]="/"
			elif(rule_area.has_override_detection_on):
				msg["rm_override"]="*"
		else:
			p.rint("[A_ws  "+time.strftime("%H:%M:%S")+"] Can't find area '"+str(area)+"' on this account, camera online?","r")
			msg["status"]="area not found"
			msg["ok"]=0

				
		# update next timestamp (the override could be timelimited) and run a rulecheck as the szenario has most certainly changed		
		rm.get_account(ws.account).update_next_ts()
		rm_check_rules(ws.account,ws.login,1)
	else:
		#return bad ack
		p.rint("[A_ws  "+time.strftime("%H:%M:%S")+"] can't find account '"+str(ws.account)+"' on rulemanager, camera online?","r")
		msg["ok"]=0
		msg["status"]="account not found"
	
	# send an update that the server has received the request
	if(msg["ok"]==1): # new override set successful, inform every viewer
		msg["area"]=enc.get("area")
		msg["account"]=ws.account
		for v in server_ws.clients:
			if(v.account==ws.account):
				msg_q_ws.append((msg,v))
	else: # update failed, send message just to the requester
		msg_q_ws.append((msg,ws))
########### HANDLE ws set override ws ################


########### HANDLE ws set color ################
def handle_ws_set_color(enc,ws):
	r = enc.get("r","0")
	g = enc.get("g","0")
	b = enc.get("b","0")
	# respond
	msg={
		"cmd": 	enc.get("cmd"),
		"r":	r,
		"g":	g,
		"b":	b
	}
	for m2m in ws.v2m:
		if(enc.get("mid")==m2m.mid):
			db.update_color(m2m,int(r),int(g),int(b),int(enc.get("brightness_pos")),int(enc.get("color_pos")))
			
			m2m.color_pos=int(enc.get("color_pos"))
			m2m.brightness_pos=int(enc.get("brightness_pos"))
			p.rint("[A_ws  "+time.strftime("%H:%M:%S")+"] '"+str(ws.login)+"' change color","v")
			msg_q_m2m.append((msg,m2m))
			break
########### HANDLE ws set color ################

########### HANDLE cam parameter ################
def handle_update_cam_parameter(enc,ws):
	in_mid=enc.get("mid",0)
	in_alarm_while_stream=enc.get("alarm_while_stream","no_alarm")
	in_resolution=enc.get("qual","HD")
	in_frame_dist=float(enc.get("fps","0.5"))
	in_area=enc.get("area","-1")
	in_alarm_ws="1"
	in_name=enc.get("name")
	in_cam=enc.get("with_cam","0")
	in_lights=enc.get("with_lights","0")
	in_pir=enc.get("with_pir","0")
	in_ext=enc.get("with_ext","0")

	# outside of loop to update cams that are offline
	db.update_cam_parameter(in_mid, in_frame_dist, in_resolution, in_alarm_while_stream, in_area, in_alarm_ws, in_name, in_cam, in_lights, in_pir, in_ext)

	# save new parameter
	found=0
	for m2m in server_m2m.clients:
		if(m2m.mid==in_mid):
			found=1
			m2m.alarm_while_streaming=in_alarm_while_stream
			m2m.resolution=in_resolution
			m2m.frame_dist=in_frame_dist
			m2m.alarm_ws=in_alarm_ws
			m2m.with_cam=in_cam
			m2m.with_lights=in_lights
			m2m.with_pir=in_pir
			m2m.with_ext=in_ext

			if(str(m2m.alias)!=str(in_name)):
				# if the name (and probably more) has change, reboot cam
				msg={}
				msg["cmd"]="set_alias"
				msg["alias"]=in_name
				msg_q_m2m.append((msg,m2m))
			elif(str(m2m.area_id)!=str(in_area)):
				# if then name has not change, but the area, just reconnect it
				# it might sound rough, but we have to reloader all the rules, 
				# change the area, inform all clients. If we just disconnect to box
				# it will redial in in 3 sec and everything will run on its own
				server_m2m.disconnect(m2m)
			else:
				# inform the webcam about the new parameter that the user has just set
				msg={}
				msg["cmd"]="update_parameter"
				msg["qual"]=m2m.resolution
				msg["alarm_while_streaming"]=m2m.alarm_while_streaming
				msg["interval"]=m2m.frame_dist
				msg["with_cam"]=m2m.with_cam
				msg["with_pir"]=m2m.with_pir
				msg["with_lights"]=m2m.with_lights
				msg["with_ext"]=m2m.with_ext
				msg_q_m2m.append((msg,m2m))
			break;

	# if not found, aka offline, just create a temporary box and send the update info
	if(found==0):
		m2m = m2m_clients("")
		m2m.mid=in_mid
		populate_m2m(m2m)
	# send this message to all ws with the same account
	for a_ws in server_ws.clients:
		if(a_ws.account == ws.account):
			connect_ws_m2m(m2m,a_ws,update_m2m=0)

	# send ok response to ws
	msg={}
	msg["cmd"]=enc.get("cmd")
	msg["ok"]=1
	msg_q_ws.append((msg,ws))
	p.rint("[A_ws  "+time.strftime("%H:%M:%S")+"] '"+str(ws.login)+"' updated cam parameter for  '"+in_mid+"'","d")
########### HANDLE cam parameter ################

########### HANDLE ws prelogin ################
def handle_ws_prelogin(enc,ws):
	msg={}
	msg["cmd"]=enc.get("cmd")
	msg["challange"]=ws.challange
	msg_q_ws.append((msg,ws))

	try:
		ip=ws.ip
	except:
		ip="???"

	p.rint2(str(ip)+" requested prelogin","l","A_ws")
	#rint("received prelogin request, sending challange "+m2m.challange)
########### HANDLE ws prelogin ################

########### HANDLE ws login ################
def handle_ws_login(enc,ws):
	global db
	msg_ws={}
	msg_ws["cmd"]=enc.get("cmd")
			
	# data base has to give us this values based on login
	db_r=db.get_ws_data(enc.get("login"))
	if(type(db_r) is int): #user not found results in return -1 or so
		#rint("db error")
		p.rint("[A_ws  "+time.strftime("%H:%M:%S")+"] '"+str(enc.get("login"))+"' not found in DB, log-in: failed","l")
		msg_ws["ok"]=-3 # not logged in
		msg_q_ws.append((msg_ws,ws))
	else:
		# generate hash for DB passwort and challange, client will generate hash_of(hash_of(passwort) + challange)
		# this enables us to save only the hash_of(password) instead of clear text password in the db!
		h = hashlib.md5()
		hashme=(str(db_r["pw"])+ws.challange).encode("UTF-8")
		h.update(hashme)
		#rint("for secure login we have: pw="+str(db_r["pw"])+" challange="+str(ws.challange)+" together="+str(hashme)+" hash="+str(h.hexdigest())+" vs received="+enc.get("client_pw"))
				
		# check parameter
		if(h.hexdigest()==enc.get("client_pw") and db_r["account"]!=""):
			# complete message
			msg_ws["ok"]=1 # logged in
			msg_q_ws.append((msg_ws,ws))
		
			# add socket infos
			ws.login=db_r["login"]
			ws.logged_in=1
			ws.account=db_r["account"]
			ws.email=db_r["email"]
			ws.uuid=enc.get("uuid","")
			ws.alarm_view=enc.get("alarm_view",0) #1,0 depending on service or app
				
			# rint and update db, as this fails when the client already disconnected, surrond with try catch
			p.ws_login(ws)
			try:
				ip=ws.ip
			except:
				ip="???"
			db.update_last_seen_ws(ws.login, ip)
				
			# search for all (active and logged-in) camera modules with the same account and tell them that we'd like to be updated
			# introduce them to each other
			for m2m in server_m2m.clients:
				if(m2m.account==ws.account):
					connect_ws_m2m(m2m,ws)
			# and finally connect all disconnected m2m to the ws
			all_m2m4account=db.get_m2m4account(ws.account)
			if(type(all_m2m4account) is int):
				p.rint("Error getting data for account "+str(ws.account),"d")
			else:	# loop over all results, and make sure that they are not online
				for all_m2m in all_m2m4account:
					if(is_mid_online(all_m2m["mid"])==0):
						# we are still here, so box is offline, lets create a empty object
						m2m = m2m_clients("")
						m2m.mid=all_m2m["mid"]
						populate_m2m(m2m)
						connect_ws_m2m(m2m,ws,update_m2m=0)
		
			# check if the same UUID has another open connection
			if(str(ws.uuid)!=""):
				for cli_ws in server_ws.clients:
					disconnect=0
					if(cli_ws.uuid==ws.uuid and cli_ws!=ws and cli_ws.login==ws.login):
						p.rint2("disconnecting '"+str(cli_ws.login)+"' / '"+str(cli_ws.uuid)+"' as that has the same UUID","d","A")
						disconnect=1
					if(cli_ws.logged_in!=1 and time.time()-cli_ws.last_comm>10*60):
						p.rint2("disconnecting an unlogged client as the last comm was "+str(time.time()-cli_ws.last_comm)+"sec ago","d","A")
						disconnect=1

					if(disconnect):
						try:
							if(hasattr(cli_ws.ws, 'sock')): 	# server v1 
								cli_ws.ws.sock.close()
							else:					# server v2
								#rint("server_ws.clients.ws.socket does not exists")
								cli_ws.ws.disconnect()
						except:
							pass
						recv_ws_con_handle("disconnect", cli_ws)

				
			# check if the user has areas which have been un-protected for a long long time
			areas=db.get_areas_for_account(ws.account)
			if(areas!=-1):
				for a in areas:
					a=a["area"]
					#rint("check for area "+a)
					data=db.get_areas_state(ws.account,a)
					if(data!=-1):
						#rint(data)
						a_ts=int(data['updated'])
						a_state=int(data['state'])
						if(a_state<=0 and (a_ts+5*86400)<time.time()):
							#rint("Area "+a+" is not active for more than 5 days")
							# area is not protected
							msg_add={}
							msg_add["cmd"]="msg"
							msg_add["msg"]="check_area,"+str(a)+",unprotected,"+str(a_ts)
							msg_q_ws.append((msg_add,ws))
			#end of long unprotection check
							
		# end of successful login
		else:
			try:
				ip=ws.ip
			except:
				ip="???"
			p.rint2("log-in from "+str(ip)+" failed for login '"+str(enc.get("login"))+"', password not correct","l","A_ws", p.bcolors.WARNING)
			msg_ws["ok"]=-2 # not logged in
			msg_q_ws.append((msg_ws,ws))
########### HANDLE ws login ################

########### HANDLE ws refresh ws ################
def handle_ws_refresh_ws(enc,ws):
	# search for all (active and logged-in) camera modules with the same account and tell them that we'd like to be updated
	# introduce them to each other
	for m2m in server_m2m.clients:
		if(m2m.account==ws.account):
			connect_ws_m2m(m2m,ws,0) # call with 0 will avoid that we append us to a list
	# and finally connect all disconnected m2m to the ws
	all_m2m4account=db.get_m2m4account(ws.account)
	if(type(all_m2m4account) is int):
		p.rint("Error getting data for account "+str(ws.account),"d")
	else:	# loop over all results, and make sure that they are not online
		for all_m2m in all_m2m4account:
			if(is_mid_online(all_m2m["mid"])==0):
				# we are still here, so box is offline, lets create a empty object
				m2m = m2m_clients("")
				m2m.mid=all_m2m["mid"]
				populate_m2m(m2m)
				connect_ws_m2m(m2m,ws,update_m2m=0)
########### HANDLE ws refresh ws ################


#***************************************************************************************#
#*********************************** End of WebSockets *********************************#
#***************************************************************************************#

#***************************************************************************************#
#**************************************** Common  **************************************#
#***************************************************************************************#
# common function more than one of ws or m2m uses

#***************************************************#
# is the m2m with this mid online?
def is_mid_online(mid):
	ret=0
	for online_m2m in server_m2m.clients:
		if(mid==online_m2m.mid):
			ret=1
			break
	return ret

#***************************************************#
# copy all infor from the database to this m2m
def populate_m2m(m2m):
	global db
	db_r=db.get_data(m2m.mid)
	# get area and account based on database value for this mid
	m2m.account=db_r["account"]
	m2m.area=db_r["area"]
	m2m.area_id=db_r["area_id"]
	m2m.alias=db_r["alias"]
	m2m.longitude=db_r["longitude"]
	m2m.latitude=db_r["latitude"]
	m2m.brightness_pos=db_r["brightness_pos"]
	m2m.color_pos=db_r["color_pos"]
	m2m.alarm_ws=db_r["alarm_ws"]
	m2m.frame_dist=float(db_r["frame_dist"])
	m2m.alarm_while_streaming=db_r["alarm_while_streaming"]
	m2m.resolution=db_r["resolution"]
	m2m.state=-1 # assume offline
	m2m.detection=db.get_areas_state(db_r["account"],db_r["area"])["state"]
	m2m.last_comm =	db_r["last_seen"]
	m2m.v_hash = db_r["v_hash"]
	m2m.v_short = db_r["v_short"]
	m2m.external_state = db_r["external_state"]
	m2m.with_cam = db_r["with_cam"]
	m2m.with_lights = db_r["with_lights"]
	m2m.with_pir = db_r["with_pir"]
	m2m.with_ext = db_r["with_ext"]
	m2m.m2m_monitor = db_r["monitor"]


#******************************************************#
# set parameter for m2m client
def set_m2m_parameter(m2m,enc,db_r,msg):
	global rm
	global db

	# override newest info, fresh from the m2m
	m2m.logged_in=1
	m2m.state=enc.get("state")
	m2m.v_hash=enc.get("v_hash","-")
	m2m.alert=alert_event() 	# TODO we should fill the alert with custom values like max photos etc
	m2m.v_short="error"
	m2m.v_sec=str(enc.get("v_sec","-"))
	try:
		m2m.v_short=str(int(str(subprocess.Popen(["git","-C", os.path.dirname(os.path.realpath(__file__)), "rev-list", "--count", m2m.v_hash],stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.PIPE).communicate()[0].decode()).replace("\n","")))
	except:
		pass
	# add info to the messge for the m2m
	msg["ok"]=1 # logged in					
	msg["alias"]=m2m.alias		# this goes to the m2m
	msg["mRed"]=db_r["mRed"]
	msg["mGreen"]=db_r["mGreen"]
	msg["mBlue"]=db_r["mBlue"]
	msg["external_state"]=db_r["external_state"]
			
	# send a second message to set the parameter for the cam, don't want to double it in the login	
	msg2={}
	msg2["cmd"]="update_parameter"
	msg2["qual"]=m2m.resolution	
	msg2["alarm_while_streaming"]=m2m.alarm_while_streaming
	msg2["interval"]=m2m.frame_dist
	msg2["with_cam"]=m2m.with_cam
	msg2["with_pir"]=m2m.with_pir
	msg2["with_lights"]=m2m.with_lights
	msg2["with_ext"]=m2m.with_ext
	msg_q_m2m.append((msg2,m2m))


	# add rules to the rule manager for this area if it wasn there before
	# first check if the account is known to the rule manager at all and add it if not
	#rint("### rm debug ###")
	#rm.print_all()
	#rint("### rm debug ###")

	if(not(rm.is_account(m2m.account))):
		#rint("account did not exist, adding")
		new_rule_account=rule_account(m2m.account)
		rm.add_account(new_rule_account)
		

	# then check the same for the area, if there was NO m2m and NO ws connected, the area wont be in the rm, otherwise it should
	if(not(rm.is_area_in_account(m2m.account,m2m.area))):
		#rint("area did not exist, adding")
		new_area=area(m2m.area,m2m.account,db) # will load rule set on its own from the database
		rm.add_area_to_account(m2m.account,new_area)

		# if the area wasn in the rule manager we have to
		# check the state, as there could be a time based trigger that wasn executed
		# lets do it for all areas for this account (kind of a waste but its is very quick)
		#rint("### rm debug ###")
		#rm.print_all()
		#rint("### rm debug ###")
		#rint("checking for this account")
		acc=rm.get_account(m2m.account)
		if(acc!=0):
			for b in acc.areas:
				detection_state=b.check_rules(1) 	# get the state, check and use db
				db.update_det("m2m",m2m.account,m2m.area,detection_state)
				#rint("area "+str(b.area)+" should be")
				#rint(detection_state)

	# get detecion state based on db
	db_r2=db.get_state(m2m.area,m2m.account)
	m2m.detection=int(db_r2["state"])
	msg["detection"]=m2m.detection

	# search for all (active and logged-in) viewers for this client (same account)
	info_viewer=0
	#rint("my m2m account is "+m2m.account)
	for viewer in server_ws.clients:
		#rint("this client has account "+viewer.account)
		if(viewer.account==m2m.account):
			# introduce them to each other
			connect_ws_m2m(m2m,viewer,1)
			info_viewer+=1
			# we could send a message to the box to tell the if there is a visitor logged in ... but they don't care
	p.m2m_login(m2m,info_viewer)
						
	try:
		ip=m2m.conn.getpeername()[0]
	except:
		ip="???"
	db.update_last_seen_m2m(m2m.mid,ip)
	db.update_m2m_version(m2m.mid, m2m.v_short, m2m.v_hash)


#******************************************************#
# when ever a websocket or a m2m device signs-on this function will be called
# the purpose is that the web socket shall be informed about the new, available client
# and the m2m shall know that there is a viewer to inform
def connect_ws_m2m(m2m,ws,update_m2m=1):
	global rm
	# add us to their (machine to viewer) list, to be notified whats going on
	if(update_m2m):
		if not(ws in m2m.m2v):
			m2m.m2v.append(ws)
		# and add them to us to give us the change to tell them if they should be sharp or not
		if not(m2m in ws.v2m):
			ws.v2m.append(m2m)

	p.connect_ws_m2m(m2m,ws)
	# send a nice and shiny message to the viewer to tell him what boxes are online,
	msg_ws2={}
	msg_ws2["cmd"]="m2v_login"
	msg_ws2["mid"]=m2m.mid
	msg_ws2["area"]=m2m.area
	msg_ws2["longitude"]=m2m.longitude
	msg_ws2["latitude"]=m2m.latitude
	msg_ws2["state"]=m2m.state
	msg_ws2["detection"]=m2m.detection
	msg_ws2["alarm_ws"]=m2m.alarm_ws
	msg_ws2["account"]=m2m.account
	msg_ws2["alias"]=m2m.alias
	msg_ws2["last_seen"]=m2m.last_comm
	msg_ws2["color_pos"]=m2m.color_pos
	msg_ws2["with_lights"]=m2m.with_lights
	msg_ws2["with_pir"]=m2m.with_pir
	msg_ws2["with_cam"]=m2m.with_cam
	msg_ws2["with_ext"]=m2m.with_ext
	msg_ws2["external_state"]=m2m.external_state
	msg_ws2["brightness_pos"]=m2m.brightness_pos
	msg_ws2["rm"]=rm.get_account(m2m.account).get_area(m2m.area).print_rules(bars=0,account_info=0,print_out=0)
	msg_ws2["open_alarms"]=db.get_open_alert_count(m2m.account,m2m.mid)
	msg_ws2["frame_dist"]=float(m2m.frame_dist)
	msg_ws2["alarm_ws"]=m2m.alarm_ws
	msg_ws2["resolution"]=m2m.resolution
	msg_ws2["alarm_while_streaming"]=m2m.alarm_while_streaming
	msg_ws2["rm_override"]=""
	if(rm.get_account(m2m.account).get_area(m2m.area).has_override_detection_off):
		 msg_ws2["rm_override"]="/"
	elif(rm.get_account(m2m.account).get_area(m2m.area).has_override_detection_on):
		msg_ws2["rm_override"]="*"

	msg_q_ws.append((msg_ws2,ws))
#******************************************************#

#******************************************************#
# this will be called if the websocket requests a webcam stream OR if he had done it before and disconnects
# purpose of this function is to ADD or REMOVE the websocket to the list "webcam" of the m2m unit and to tell
# the cam at what speed it shall run. BTW: there is a problem with the KGV .. but not important.
def set_webcam_con(mid,on_off,ws):
	#rint("--> change on_off "+str(on_off))
	on_off=int(on_off)
	msg={}
	msg["cmd"]="set_interval"
	#search for the m2m module that shall upload the picture to the ws

	for m2m in ws.v2m:
		if(m2m.mid==mid):
			# yes, we change he quality for everyone... too bad
			msg["qual"]=m2m.resolution	
			msg["alarm_while_streaming"]=m2m.alarm_while_streaming

			#rint("habe die angeforderte MID in der clienten liste vom ws gefunden")
			# thats our m2m cam
			if(on_off>0):
				# reset counter
				ws.webcam_countdown=99;

				# scan if we are already in the webcam list, and remove us if so
				for wcv in m2m.webcam:
					if(wcv.ws==ws):
						m2m.webcam.remove(wcv)

				# put the ws on his list of webcam subscripters
				viewer=webcam_viewer(ws)
				viewer.interval=m2m.frame_dist
				viewer.ts=0 # deliver the next frame asap
				m2m.webcam.append(viewer) 

				msg["interval"]=m2m.frame_dist
				# inform the webcam that we are watching
				msg_q_m2m.append((msg,m2m))

				p.rint("[A_ws  "+time.strftime("%H:%M:%S")+"] Added "+str(ws.login)+" to webcam stream from '"+str(m2m.alias)+"' "+str(mid),"c")
					
			# this clients switched off 
			else:
				# remove us from the list 
				# go through all elements in the webcam list 
				#rint("suche in der webcam liste nach unserem ws")
				for viewer in m2m.webcam:
					if(viewer.ws==ws):
						#rint("gefunden und entfernt")
						m2m.webcam.remove(viewer)

						# check if we shall switch of the feed
						clients_remaining=len(m2m.webcam)
						if(clients_remaining==0):
							msg["interval"]=0
							#rint("sende stop nachricht an m2m:"+str(m2m.mid))
						msg_q_m2m.append((msg,m2m))
						p.rint("[A_ws  "+time.strftime("%H:%M:%S")+"] Removed "+str(ws.login)+" from webcam stream of '"+str(m2m.alias)+"' "+str(m2m.mid)+" ("+str(clients_remaining)+" ws left)","c")
#******************************************************#

#******************************************************#
# get_challange is used to avoid that the clients send their password or even an encripted version of it.
# first they request a challange (generated by this function) which will be send back to the client, than
# the client will append his password to it and encode that. this makes sure that the loginpw is different
# at all logins and nobody can just repeat the login sequence
def get_challange(size=12, chars=string.ascii_uppercase + string.digits):
	return ''.join(random.choice(chars) for _ in range(size))
#******************************************************#

#******************************************************#
# this function will be called in the main loop and shall check if there is a client in the state that he
# started to capture a few images and might be ready to send them via mail / notification
def check_alerts():
	ret=-1
	for cli in server_m2m.clients:
		if(cli.alert.notification_send==0 and cli.alert.collecting==1):
			# found client in "alert but not yet notified" state, see if it is time to notify
			send = 0
			# if the gab between the last_upload and now is > timeout, last_upload will be set every time a file arrives, and initialized to 0 once the state changes to alert
			if(cli.alert.last_upload!=0):
				if(time.time()>cli.alert.last_upload+cli.alert.file_max_timeout_ms/1000):
					#rint("last upload ist old enough")
					send=1
			# or enough pictures have been uploaded
			if(len(cli.alert.files)>=cli.alert.files_expected):
				#rint("found enough files")
				send=1

			# fire in the hole
			if(send==1):
				# email?
				if(cli.alert.comm_path % 2 == 1):
					#rint("sending mail")
					#send_mail.send( subject, text, files=[], send_to="KKoolljjaa@gmail.com",send_from="koljasspam493@gmail.com", server="localhost"):
					# send a mail
					#send_mail.send("alert", "oho", cli.alert.files)

					# send a notification to all clients
					msg={}
					msg["cmd"]="update_open_alerts"
					msg["mid"]=cli.mid
					msg["open_alarms"]=db.get_open_alert_count(cli.account,cli.mid)
					for viewer in cli.m2v:
						msg_q_ws.append((msg,viewer))


					cli.alert.notification_send_ts=time.time()
					p.rint("[A_m2m "+time.strftime("%H:%M:%S")+"] '"+str(cli.mid)+"' triggered Email","a")
					ret=0
					cli.alert.collecting=0
					cli.alert.notification_send=1
	return ret
#******************************************************#

#******************************************************#
# this fuction shall be called if the environment changes. E.g. if a ws client change the location.
# it will load the rule_account from the rule manager by  the given "account" string and go through all areas,
# associated the this account. it will evaluate the rules for each area of the account and write the status to the database.
# after that it will go through the complete m2m_client list and grab every box that has the same accont, reload the status
# of the box from the database and send it to the box.
# the login argument is used to keep track what ws client has triggered all that changes
def rm_check_rules(account,login,use_db):
	global rm
	global db

	#rint("### rm debug ###")
	#rm.print_all()
	#rint("### rm debug ###")

	# get account from rulemanager
	acc=rm.get_account(account)
	if(acc!=0 and acc.account!=""):
		# run the rule check for every area in this account
		#rint("running rule check on every area of this account")
		for b in acc.areas:
			if(b.area!=""):
				detection_state=b.check_rules(use_db) 	# get the rule state, 1 for detection on and 0 for off ... this is NOT the detection state the box shall get (could be 2)
				if(detection_state): # if the alert should be "on", grab the first box you can find in this account and area and check what the detection_on_mode is to set it to 1 or 2
					real_detection_state=1 # backup
					for m2m in server_m2m.clients:
						if(m2m.account==account and m2m.area==b.area):
							real_detection_state=m2m.detection_on_mode
							break
				else:
					real_detection_state=0
				db.update_det(login,account,b.area,real_detection_state)
			#rint("updateing to db that detection of area "+str(b.area)+" should be")
			#rint(detection_state)

		# get all areas in this account, we have to check if we have at least on box in each area online, those ares with all boxes offline need to send a fake message to the ws to show the status change
		dead_areas_on_account=db.get_areas_for_account(account)
			
		# send an update to every box in this account which has to change the status?
		#rint("now we have to check for every box what there detection status shall be and send it to them")
		for m2m in server_m2m.clients:
			if(m2m.account==account):
				for dead_area in dead_areas_on_account: 
					if(dead_area["area"]==m2m.area):
						dead_areas_on_account.remove(dead_area)
				#rint("checkin for box "+m2m.alias+" in area "+m2m.area)
				db_r2=db.get_state(m2m.area,account)
				if(type(db_r2)!=int):
					#rint("will I send that detection state should be "+str(db_r2["state"])+"?")
					#rint("because m2m.detection is: "+str(m2m.detection))
					detection=int(db_r2["state"])
					if(m2m.detection!=detection):
						m2m.detection=detection
						msg={}
						msg["cmd"]="set_detection"
						msg["state"]=detection
						# step 8 append message for this m2m client to go sharp ;)
						msg_q_m2m.append((msg,m2m))
						# step 9 tell the watching ws that it went sharp		
						affected_ws_clients=0
						p.rint("[A_RM  "+time.strftime("%H:%M:%S")+"] ->(M2M) set detection of m2m '"+str(m2m.mid)+"' in area "+str(m2m.area)+" to '"+str(det_state[int(db_r2["state"])])+"'","a")
						#break DO NOT! MIGHT HAVE MULTIPLE BOXES
				else:
					p.err("[A_RM  "+time.strftime("%H:%M:%S")+"] get_state return an int, being called at rm_check")

				# step 9: even if the detection might have not changed, the rm might have. 
				# send an updated state to all ws clients of this m2m box
				msg2={}
				msg2["cmd"]="state_change"
				msg2["account"]=m2m.account	
				msg2["area"]=m2m.area
				msg2["state"]=m2m.state
				msg2["detection"]=m2m.detection
				msg2["alarm_ws"]=m2m.alarm_ws
				msg2["rm"]=acc.get_area(m2m.area).print_rules(bars=0,account_info=0,print_out=0)
				for ws in m2m.m2v:
					msg_q_ws.append((msg2,ws))

		# generate fake update messages for areas without clients to toggle status for this area
		if(len(dead_areas_on_account)):
			all_m2m_on_account=db.get_m2m4account(account) # get all  m2m boxes for this account
			for dead_area in dead_areas_on_account: # all remaining dead_area, which haven't been update in the last step
				for db_m2m in all_m2m_on_account: # find a box that is in the non-updated-area
					if(db_m2m["area"]==dead_area["area"]):
						# remove this area from the dead_areas_on_account
						dead_areas_on_account.remove(dead_area)

						# generate object
						m2m_temp = m2m_clients("")
						m2m_temp.mid=db_m2m["mid"]
						# fill it with values from db, which updates the state
						populate_m2m(m2m_temp)

						# generate status change message
						msg={
							"mid":		m2m_temp.mid,
							"cmd":		"state_change",
							"alarm_ws":	m2m_temp.alarm_ws,
							"state":	m2m_temp.state,
							"area":		m2m_temp.area,
							"account":	m2m_temp.account,
							"detection":	m2m_temp.detection,
							"rm":		rm.get_account(m2m_temp.account).get_area(m2m_temp.area).print_rules(bars=0,account_info=0,print_out=0)
						}
						# now go through all ws clients to check which one is on the same account, and connect them
						for ws in server_ws.clients:
							if(ws.account == account):
								msg_q_ws.append((msg,ws))
						break #for m2m in all_m2m_on_account
			
#******************************************************#

#******************************************************#
# this is the call back function that the p process uses whenever the user typed "ENTER". we'll react by putting some debug output on the terminal.
# as this code has all the variables we just call the Displaying function in p with our variables ..
def helper_output(input):
	print("")
	
	if(input=="rm"):
		p.show_m2m(1,0,0)
		rm.print_all()
		p.show_m2m(1,0,0)
	
	elif(input=="ws"):
		p.show_ws(-2,len(server_ws.clients),0)
		p.show_ws(-1,0,0)
		for ws in server_ws.clients:
			p.show_ws(0,0,ws)
		p.show_ws(1,0,0)

	elif(input=="m2m"):
		p.show_m2m(-2,len(server_m2m.clients),0)
		p.show_m2m(-1,0,0)
		for m2m in server_m2m.clients:
			p.show_m2m(0,0,m2m)
		p.show_m2m(1,0,0)
	
	elif(input=="quit"):
		print("shutting down")
		sys.exit()
	
	else:
		print("whoot? ->"+input+"<-")
		print("your choices are:")
		print("m2m: to print informations about the connected camera clients")
		print("ws: to print informations about the connected websocket clients")
		print("rm: to print informations about the rule manager")

	print("")
#******************************************************#
#***************************************************************************************#
#************************************ End of Common  ***********************************#
#***************************************************************************************#

#***************************************************************************************#
#************************************** Variables **************************************#
#***************************************************************************************#

# our helper for the console
p.start()
p.subscribe_callback(helper_output)
# M2M structures
recv_m2m_msg_q=[]	# incoming
recv_m2m_con_q=[]	# incoming
msg_q_m2m=[]		# outgoing
server_m2m.start()
server_m2m.subscribe_callback(recv_m2m_msg_q_handle,"msg")
server_m2m.subscribe_callback(recv_m2m_con_q_handle,"con")

# WS structures
recv_ws_msg_q=[]	# incoming
recv_ws_con_q=[]	# incoming
msg_q_ws=[] 		# outgoing
server_ws.subscribe_callback(recv_ws_msg_q_handle,"msg")
server_ws.subscribe_callback(recv_ws_con_q_handle,"con")
server_ws.start()

# DB Structure used for the login
db=sql()
db.connect()

# our rule set maanger for all clients. Argument is the callback function
rm = rule_manager(db)

# debug timing
debug_in=debug("in")
debug_out=debug("out")
debug_loading_assist=loading_assist(server_ws,server_m2m)

# else
busy=1
last_rulecheck_ts=0


# document root
upload_dir=os.path.join(os.path.dirname(os.path.realpath(__file__)),"..","uploads")
if(not(os.path.isdir(upload_dir))):
	p.rint2("Creating upload dir","v","A Server")
	os.mkdir(upload_dir)

#***************************************************************************************#
#********************************** End of Variables ***********************************#
#***************************************************************************************#

#***************************************************************************************#
#************************************** Main loop **************************************#
#***************************************************************************************#
last_ws_bh_time=0
CPU_DEBUG=0
if(CPU_DEBUG):
	task=[0,0,0,0,0,0,0,0];
	task_timer=time.time()
	task_loop=0

while 1:
	# sleeping
	if(busy==0):
		time.sleep(0.03)
	busy=0
	
	if(CPU_DEBUG):
		if(task_loop>1000):
			print(str(0.1*round(1000*(1-(((task_loop-sum(task))*0.03)/(time.time()-task_timer)))))+"% load, taskcounter:", end="")
			print(task)
			task_loop=0
			task_timer=time.time()
			task=[0,0,0,0,0,0,0,0];
		task_loop+=1

	############### recv ###################
	if(recv_m2m_con_dq_handle()==1): #returns 1 if there was a connection change by a m2m unit
		if(CPU_DEBUG):
			task[0]+=1
		busy=1

	if(recv_m2m_msg_dq_handle()==1): #returns 1 if there was a message to receive from a m2m unit
		if(CPU_DEBUG):
			task[1]+=1
		busy=1

	if(recv_ws_con_dq_handle()==1): #returns 1 if there was a connection change by a web socket client
		if(CPU_DEBUG):
			task[2]+=1
		busy=1

	if(recv_ws_msg_dq_handle()==1):  #returns 1 if there was a message to receive from a web socket client
		if(CPU_DEBUG):
			task[3]+=1
		busy=1

	############## send ###################
	if(snd_m2m_msg_dq_handle()==1): #returns 1 if there was a message to send
		if(CPU_DEBUG):
			task[4]+=1
		busy=1

	if(snd_ws_msg_dq_handle()==1): #returns 1 if there was a message to send
		if(CPU_DEBUG):
			task[5]+=1
		busy=1

	############## maintenance ###################
	# check if we have clients in the alert state ready to send a mail or so
	if(check_alerts()==0):
		if(CPU_DEBUG):
			task[6]+=1
		busy=1

	# send a heartbeat to the clients
	debug_loading_assist.check(msg_q_ws)

	# check the rules
	if(time.time()>last_rulecheck_ts+60): # this is not a good way .. we should know when we have to call it for a timebased change, not guess it
		if(CPU_DEBUG):
			task[7]+=1
		busy=1
		last_rulecheck_ts=time.time()
		p.rint("[A_RM  "+time.strftime("%H:%M:%S")+"] running periodically 60 sec check","r")
		now=time.localtime()[3]*3600+time.localtime()[4]*60+time.localtime()[5]
		#rint(time.localtime()[3]*3600+time.localtime()[4]*60+time.localtime()[5])
		for acc in rm.data:
			if(now>acc.next_ts or acc.check_day_jump()): # next_ts hold the time when a rule will change
				p.rint("[A_RM  "+time.strftime("%H:%M:%S")+"] full rule_check for account "+str(acc.account)+" required","r")
				rm_check_rules(acc.account,"timetrigger",1) # check with database
				# reset next ts and check again, as this rule is "over"
				acc.next_ts=-1
				acc.update_next_ts()
		debug_ts=time.time()-last_rulecheck_ts
		p.rint("[A_RM  "+time.strftime("%H:%M:%S")+"] Check took "+str(debug_ts),"r")

		# sneak in the "client still there"-check
		server_m2m.check_clients()			
#***************************************************************************************#
#********************************** End of Main loop ***********************************#
#***************************************************************************************#
